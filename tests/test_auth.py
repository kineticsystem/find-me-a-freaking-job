"""Login: no token, no data; a token names its user; the admin routes are the admin's."""

from __future__ import annotations

import pytest

from jobfinder import auth, db
from jobfinder.config import DEFAULT_USER_ID


@pytest.fixture()
def client(anon_client):
    """This file drives login itself: the anonymous client, no header."""
    return anon_client


@pytest.fixture()
def accounts(client):
    db.claim_user(DEFAULT_USER_ID, "Admin@Example.com", auth.hash_password("admin-pass-1"), is_admin=True)
    other = db.create_user("other@example.com", auth.hash_password("other-pass-1"))
    return {"admin": DEFAULT_USER_ID, "other": other}


def _login(client, email, password):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_everything_but_health_and_login_needs_a_token(client):
    assert client.get("/health").status_code == 200
    for path in ("/jobs", "/jobs/facets", "/preferences", "/settings", "/sources", "/auth/me", "/users"):
        assert client.get(path).status_code == 401, path
    assert client.get("/jobs", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    assert client.get("/jobs", params={"token": "nonsense"}).status_code == 401   # never in the URL


def test_password_is_hashed_and_token_is_hashed(client, accounts):
    headers = _login(client, "admin@example.com", "admin-pass-1")   # email case-insensitive
    token = headers["Authorization"].split()[1]
    with db.connect() as conn:
        stored = conn.execute("SELECT password_hash FROM users WHERE id = ?", (accounts["admin"],)).fetchone()[0]
        rows = [r[0] for r in conn.execute("SELECT token_hash FROM api_tokens")]
    assert stored.startswith("$argon2id$") and "admin-pass-1" not in stored
    assert token not in rows and len(rows) == 1
    assert client.get("/auth/me", headers=headers).json()["email"] == "admin@example.com"
    assert "password_hash" not in client.get("/auth/me", headers=headers).json()


def test_wrong_password_and_unknown_email_look_the_same(client, accounts):
    a = client.post("/auth/login", json={"email": "admin@example.com", "password": "wrong"})
    b = client.post("/auth/login", json={"email": "nobody@example.com", "password": "wrong"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_logout_revokes_only_that_token(client, accounts):
    laptop = _login(client, "admin@example.com", "admin-pass-1")
    phone = _login(client, "admin@example.com", "admin-pass-1")
    assert client.post("/auth/logout", headers=laptop).status_code == 200
    assert client.get("/auth/me", headers=laptop).status_code == 401
    assert client.get("/auth/me", headers=phone).status_code == 200
    assert client.post("/auth/logout", params={"everywhere": "true"}, headers=phone).status_code == 200
    assert client.get("/auth/me", headers=phone).status_code == 401


def test_expired_token_is_refused_and_removed(client, accounts):
    headers = _login(client, "admin@example.com", "admin-pass-1")
    with db.connect() as conn:
        conn.execute("UPDATE api_tokens SET expires_at = '2000-01-01T00:00:00+00:00'")
    assert client.get("/auth/me", headers=headers).status_code == 401
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 0


def test_decisions_and_preferences_follow_the_token(client, accounts, seeded):
    admin = _login(client, "admin@example.com", "admin-pass-1")
    other = _login(client, "other@example.com", "other-pass-1")
    client.patch(f"/jobs/{seeded['a']}/state", json={"status": "applied"}, headers=admin)
    assert client.get(f"/jobs/{seeded['a']}", headers=admin).json()["status"] == "applied"
    assert client.get(f"/jobs/{seeded['a']}", headers=other).json()["status"] == "new"
    assert [e["score"] for e in client.get(f"/jobs/{seeded['a']}", headers=admin).json()["evaluations"]] == [92, 85]
    assert client.get(f"/jobs/{seeded['a']}", headers=other).json()["evaluations"] == []
    # CV and notes are theirs too
    client.put("/profile/notes", json={"text": "Gardens, not servers."}, headers=other)
    client.post("/profile/cv", files={"file": ("cv.md", b"# Sam\nHead gardener.", "text/markdown")}, headers=other)
    assert client.get("/profile", headers=other).json()["notes"] == "Gardens, not servers.\n"
    assert client.get("/profile", headers=admin).json()["notes"] == ""
    assert client.get("/profile/cv", headers=other).content == b"# Sam\nHead gardener."
    assert client.get("/profile/cv", headers=admin).status_code == 404
    client.put("/preferences", json={"based_in": "Elsewhere", "titles": ["Gardener"]}, headers=other)
    assert client.get("/preferences", headers=other).json()["preferences"]["titles"] == ["Gardener"]
    assert client.get("/preferences", headers=admin).json()["preferences"]["titles"] != ["Gardener"]


def test_admin_routes_are_admin_only(client, accounts):
    admin = _login(client, "admin@example.com", "admin-pass-1")
    other = _login(client, "other@example.com", "other-pass-1")
    for path in ("/settings", "/sources", "/users", "/runs"):
        assert client.get(path, headers=other).status_code == 403, path
        assert client.get(path, headers=admin).status_code == 200, path
    assert client.get("/profile", headers=other).status_code == 200      # their own CV and notes
    assert client.post("/users", json={"email": "x@example.com", "password": "12345678"}, headers=other).status_code == 403


def test_admin_manages_accounts(client, accounts):
    admin = _login(client, "admin@example.com", "admin-pass-1")
    r = client.post("/users", json={"email": "new@example.com", "password": "new-pass-11"}, headers=admin)
    assert r.status_code == 200 and r.json()["user"]["is_admin"] is False
    uid = r.json()["user"]["id"]
    assert client.post("/users", json={"email": "new@example.com", "password": "new-pass-11"}, headers=admin).status_code == 409
    assert client.post("/users", json={"email": "bad", "password": "new-pass-11"}, headers=admin).status_code == 422
    assert client.post("/users", json={"email": "s@example.com", "password": "short"}, headers=admin).status_code == 422
    fresh = _login(client, "new@example.com", "new-pass-11")
    assert client.put(f"/users/{uid}/password", json={"password": "reset-pass-1"}, headers=admin).status_code == 200
    assert client.get("/auth/me", headers=fresh).status_code == 401           # reset logs them out
    _login(client, "new@example.com", "reset-pass-1")
    emails = [u["email"] for u in client.get("/users", headers=admin).json()["users"]]
    assert emails == ["admin@example.com", "other@example.com", "new@example.com"]
    assert client.delete(f"/users/{accounts['admin']}", headers=admin).status_code == 400
    assert client.delete(f"/users/{uid}", headers=admin).status_code == 200
    assert client.post("/auth/login", json={"email": "new@example.com", "password": "reset-pass-1"}).status_code == 401


def test_user_changes_own_password_and_keeps_this_device(client, accounts):
    laptop = _login(client, "other@example.com", "other-pass-1")
    phone = _login(client, "other@example.com", "other-pass-1")
    bad = client.put("/auth/password", json={"current_password": "wrong", "new_password": "other-pass-2"}, headers=laptop)
    assert bad.status_code == 401
    ok = client.put("/auth/password", json={"current_password": "other-pass-1", "new_password": "other-pass-2"}, headers=laptop)
    assert ok.status_code == 200
    assert client.get("/auth/me", headers=laptop).status_code == 200
    assert client.get("/auth/me", headers=phone).status_code == 401
    _login(client, "other@example.com", "other-pass-2")


def test_first_account_is_set_up_once_and_owns_existing_data(client, seeded):
    assert client.get("/health").json()["needs_setup"] is True
    r = client.post("/auth/setup", json={"email": "first@example.com", "password": "first-pass-1"})
    assert r.status_code == 200 and r.json()["user"]["is_admin"] is True and r.json()["user"]["id"] == DEFAULT_USER_ID
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.get("/jobs", headers=headers).json()["jobs"][0]["score"] == 92
    assert client.get("/health").json()["needs_setup"] is False
    assert client.post("/auth/setup", json={"email": "second@example.com", "password": "second-pass-1"}).status_code == 409
