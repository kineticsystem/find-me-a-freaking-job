"""Sources: the registry is shared, following is per user (plan, Decision 2)."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import make_job

from jobfinder import auth, db, discovery
from jobfinder.config import DEFAULT_USER_ID
from jobfinder.models import RawJob


@pytest.fixture()
def two(tmp_db, criteria):
    db.claim_user(DEFAULT_USER_ID, "a@example.com", auth.hash_password("password-1"), is_admin=True)
    db.upsert_source({"id": "seed-board", "type": "remoteok", "enabled": True}, origin="config")
    b = db.create_user("b@example.com", auth.hash_password("password-1"))
    return b


def test_seed_sources_are_followed_by_everyone_including_later_accounts(two):
    assert db.source_followers("seed-board") == [1, two]
    db.upsert_source({"id": "seed-two", "type": "remotive", "enabled": True}, origin="config")   # added to the YAML later
    assert db.source_followers("seed-two") == [1, two]


def test_a_source_added_by_one_user_is_theirs_alone_and_shared_on_second_add(two):
    db.upsert_source({"id": "gh-acme", "type": "greenhouse", "slug": "acme"}, origin="user", followers=[two])
    assert db.source_followers("gh-acme") == [two]
    mine = {s["id"]: s for s in db.list_sources(two)}
    theirs = {s["id"]: s for s in db.list_sources(1)}
    assert mine["gh-acme"]["following"] == 1
    assert "gh-acme" not in theirs                            # not in user 1's list at all
    assert db.upsert_source({"id": "gh-acme", "type": "greenhouse", "slug": "acme"}, origin="user", followers=[1]) is False
    assert db.source_followers("gh-acme") == [two]           # a re-add does not follow; follow_source does
    db.follow_source(1, "gh-acme")
    assert db.source_followers("gh-acme") == [1, two]


def test_fetched_only_while_someone_follows_and_following_revives_a_failed_board(two):
    ids = lambda: [s["id"] for s in db.active_sources()]  # noqa: E731
    assert ids() == ["seed-board"]
    db.follow_source(1, "seed-board", False)
    assert ids() == ["seed-board"]                           # user two still follows
    db.follow_source(two, "seed-board", False)
    assert ids() == []                                       # nobody: not fetched
    for _ in range(5):
        db.record_source_result("seed-board", 0, "boom")
    db.follow_source(1, "seed-board", True)
    assert ids() == ["seed-board"] and db.get_source("seed-board")["fail_count"] == 0


def test_postings_are_visible_and_scored_only_for_followers(two, criteria):
    db.upsert_source({"id": "gh-acme", "type": "greenhouse", "slug": "acme"}, origin="user", followers=[two])
    with db.connect() as conn:
        shared, _ = db.upsert_job(conn, make_job("Shared Co", "Engineer", source_id="seed-board"))
        private, _ = db.upsert_job(conn, make_job("Acme", "Gardener", source_id="gh-acme"))
    assert [j["id"] for j in db.list_jobs(criteria_hash=criteria, user_id=1)[0]] == [shared]
    assert sorted(j["id"] for j in db.list_jobs(criteria_hash=criteria, user_id=two)[0]) == [shared, private]
    assert [r["id"] for r in db.jobs_needing("triage", criteria, 10, user_id=1)] == [shared]
    assert sorted(r["id"] for r in db.jobs_needing("triage", criteria, 10, user_id=two)) == [shared, private]
    assert db.stats(user_id=1)["jobs"] == 1 and db.stats(user_id=two)["jobs"] == 2
    assert db.facets(user_id=1)["source"] == {"seed-board": 1}
    assert db.facets(user_id=two)["source"] == {"seed-board": 1, "gh-acme": 1}
    # switching a board off hides its postings -- except what you already decided on
    db.set_user_state(private, "shortlisted", user_id=two)
    db.follow_source(two, "gh-acme", False)
    assert [j["id"] for j in db.list_jobs(criteria_hash=criteria, user_id=two)[0]] == [private, shared][::-1] or \
           sorted(j["id"] for j in db.list_jobs(criteria_hash=criteria, user_id=two)[0]) == [shared, private]
    db.set_user_state(private, "new", user_id=two)
    with db.connect() as conn:
        conn.execute("DELETE FROM user_state WHERE job_id = ?", (private,))
    assert [j["id"] for j in db.list_jobs(criteria_hash=criteria, user_id=two)[0]] == [shared]


def test_the_same_posting_from_two_boards_is_one_job_seen_by_both_sides(two):
    from jobfinder.pipeline import fetch

    db.upsert_source({"id": "gh-acme", "type": "greenhouse", "slug": "acme"}, origin="user", followers=[two])
    raw = [RawJob(source_id="seed-board", company="Acme", title="Engineer", location="Remote", url="https://a/1", description="x" * 50),
           RawJob(source_id="gh-acme", company="Acme", title="Engineer", location="Remote", url="https://a/2", description="x" * 50)]
    out = fetch.store(raw, 100)
    assert out["stored"] == 1 and out["new"] == 1
    with db.connect() as conn:
        assert sorted(r["source_id"] for r in conn.execute("SELECT source_id FROM job_sources")) == ["gh-acme", "seed-board"]
    assert db.stats(user_id=1)["jobs"] == 1 and db.stats(user_id=two)["jobs"] == 1


def test_discovered_boards_inherit_the_followers_of_where_they_were_found(two, monkeypatch):
    monkeypatch.setattr(discovery.settings().discovery, "enabled", True)
    db.upsert_source({"id": "gh-acme", "type": "greenhouse", "slug": "acme"}, origin="user", followers=[two])
    jobs = [RawJob(source_id="seed-board", company="X", title="Y", location="", url="https://boards.greenhouse.io/everyone/jobs/1", description=""),
            RawJob(source_id="gh-acme", company="X", title="Y", location="", url="https://jobs.lever.co/onlytwo/abc", description="")]
    found = discovery.harvest(jobs)
    assert sorted(found) == ["gr-everyone", "le-onlytwo"]
    assert db.source_followers("gr-everyone") == [1, two]
    assert db.source_followers("le-onlytwo") == [two]


def test_api_sources_are_per_user(anon_client, two):
    from jobfinder import auth as auth_mod
    def login(email):
        r = anon_client.post("/auth/login", json={"email": email, "password": "password-1"})
        return {"Authorization": f"Bearer {r.json()['token']}"}
    a, b = login("a@example.com"), login("b@example.com")
    db.upsert_source({"id": "gh-acme", "type": "greenhouse", "slug": "acme"}, origin="user", followers=[two])
    rows = {s["id"]: s for s in anon_client.get("/sources", headers=a).json()["sources"]}
    assert rows["seed-board"]["following"] == 1 and rows["seed-board"]["deletable"] is False
    assert "gh-acme" not in rows                                                       # somebody else's: invisible
    assert anon_client.delete("/sources/gh-acme", headers=a).status_code == 404
    assert anon_client.post("/sources/gh-acme/enabled", headers=a).status_code == 404   # cannot follow what you cannot see
    assert db.source_followers("gh-acme") == [two]
    assert anon_client.delete("/sources/seed-board", headers=b).status_code == 400
    assert anon_client.post("/sources/seed-board/enabled", params={"enabled": "false"}, headers=a).status_code == 200
    assert db.source_followers("seed-board") == [two]
    # pasting the same URL attaches to the existing row; removing is per list, the row lives while anyone has it
    db.follow_source(1, "gh-acme")
    r = anon_client.delete("/sources/gh-acme", headers=b)
    assert r.status_code == 200 and r.json()["dropped_for_everyone"] is False
    assert "gh-acme" not in {s["id"] for s in anon_client.get("/sources", headers=b).json()["sources"]}
    assert db.get_source("gh-acme") is not None and db.source_followers("gh-acme") == [1]
    r = anon_client.delete("/sources/gh-acme", headers=a)
    assert r.status_code == 200 and r.json()["dropped_for_everyone"] is True
    assert db.get_source("gh-acme") is None


def test_old_database_every_user_follows_every_source(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "db_path", lambda: path)
    db.init_db()
    db.upsert_source({"id": "on", "type": "remoteok", "enabled": True}, origin="config")
    db.upsert_source({"id": "off", "type": "remotive", "enabled": False}, origin="discovered")
    with db.connect() as conn:
        db.upsert_job(conn, make_job("X", "Y", source_id="on"))
        conn.executescript("DROP TABLE user_sources; DROP TABLE job_sources;")   # the pre-following shape
    db.init_db()
    assert db.source_followers("on") == [1] and db.source_followers("off") == []
    assert {s["id"]: s["following"] for s in db.list_sources(1)} == {"on": 1, "off": 0}
    with db.connect() as conn:
        assert conn.execute("SELECT source_id FROM job_sources").fetchall()[0][0] == "on"
