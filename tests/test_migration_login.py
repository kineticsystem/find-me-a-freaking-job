"""A database from before login (users without email) is upgraded in place."""

from __future__ import annotations

import sqlite3

from jobfinder import auth, db


def test_pre_login_database_gains_accounts(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "db_path", lambda: path)
    db.init_db()
    with sqlite3.connect(path) as conn:                       # strip it back to the pre-login shape
        conn.executescript("""
            DROP TABLE api_tokens;
            CREATE TABLE users_old (id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO users_old SELECT id, name, created_at FROM users;
            DROP TABLE users; ALTER TABLE users_old RENAME TO users;
        """)
    db.init_db()                                              # the upgrade
    users = db.list_users()
    assert [u["id"] for u in users] == [1] and users[0]["is_admin"] and not users[0]["can_login"]
    assert not db.any_user_can_login()
    db.claim_user(1, "me@example.com", auth.hash_password("password-1"), is_admin=True)
    assert db.get_user_by_email("me@example.com")["id"] == 1
    db.init_db()                                              # idempotent
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'idx_users_email'").fetchone()[0] == 1
