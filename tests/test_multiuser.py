"""Per-user scores and decisions (multi-user plan, after Decision 1)."""

import sqlite3

import pytest

from jobfinder import db


def test_old_single_user_database_is_migrated_and_nothing_is_lost(tmp_path, monkeypatch):
    """A database from before user_id existed: user_state keyed by job only,
    evaluations without the column. After init_db every row belongs to the
    default user and every decision and score is still there."""
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "db_path", lambda: path)
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE jobs (id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, source_id TEXT NOT NULL,
            company TEXT NOT NULL, title TEXT NOT NULL, location TEXT, remote_type TEXT, url TEXT, apply_url TEXT,
            description TEXT, salary_raw TEXT, posted_at TEXT, tags TEXT, first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL, seen_count INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL, stats TEXT, error TEXT);
        CREATE TABLE evaluations (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            run_id INTEGER, stage TEXT NOT NULL, criteria_hash TEXT NOT NULL, score INTEGER NOT NULL, verdict TEXT NOT NULL,
            eligible INTEGER, summary TEXT, eligibility TEXT, salary TEXT, tech_stack TEXT, concerns TEXT, rationale TEXT,
            model TEXT, created_at TEXT NOT NULL);
        CREATE TABLE user_state (job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'new', notes TEXT, reason TEXT, updated_at TEXT NOT NULL);
        INSERT INTO jobs (id, fingerprint, source_id, company, title, first_seen, last_seen) VALUES
            (1, 'f1', 's', 'Acme', 'Engineer', 't', 't'), (2, 'f2', 's', 'Globex', 'Developer', 't', 't');
        INSERT INTO evaluations (job_id, stage, criteria_hash, score, verdict, created_at) VALUES (1, 'triage', 'c', 80, 'strong', 't');
        INSERT INTO user_state (job_id, status, reason, updated_at) VALUES (1, 'applied', NULL, 't'), (2, 'dismissed', 'agency', 't');
    """)
    old.commit(); old.close()

    db.init_db()
    with db.connect() as conn:
        assert {r["name"] for r in conn.execute("PRAGMA table_info(evaluations)")} >= {"user_id"}
        pk = [r["name"] for r in conn.execute("PRAGMA table_info(user_state)") if r["pk"]]
        assert sorted(pk) == ["job_id", "user_id"]
        assert conn.execute("SELECT user_id, score FROM evaluations").fetchall()[0][:] == (1, 80)
        assert [(r["job_id"], r["user_id"], r["status"], r["reason"]) for r in conn.execute("SELECT * FROM user_state ORDER BY job_id")] == \
            [(1, 1, "applied", None), (2, 1, "dismissed", "agency")]
    # the queries see the migrated rows as the default user's
    rows, total = db.list_jobs(criteria_hash="c")
    assert total == 1 and rows[0]["status"] == "applied" and rows[0]["score"] == 80   # the dismissed one is hidden
    assert db.list_jobs(criteria_hash="c", hidden=True)[1] == 1
    assert db.recent_rejections()[0]["reason"] == "agency"
    db.init_db()  # idempotent


@pytest.fixture()
def two_users(seeded, criteria):
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO users (id, name, created_at) VALUES (2, 'second', 't')")
    return seeded


def test_scores_and_decisions_are_per_user(two_users, criteria):
    s = two_users
    # user 2 scores job c highly and dismisses job a; user 1 sees none of that
    with db.connect() as conn:
        db.record_evaluation(conn, job_id=s["c"], run_id=None, stage="triage", criteria_hash=criteria,
                             score=95, verdict="strong", model="m", user_id=2)
    db.set_user_state(s["a"], "dismissed", reason="not for user two", user_id=2)

    u1, _ = db.list_jobs(criteria_hash=criteria, sort="score")
    u2, _ = db.list_jobs(criteria_hash=criteria, sort="score", user_id=2)
    assert [(j["id"], j["score"]) for j in u1] == [(s["a"], 92), (s["b"], 55), (s["c"], None)]   # unchanged for user 1
    assert [(j["id"], j["score"]) for j in u2] == [(s["c"], 95), (s["b"], None)]                   # a is dismissed for user 2
    assert db.recent_rejections(user_id=1) == [] and db.recent_rejections(user_id=2)[0]["reason"] == "not for user two"

    # "needs a score" is per user: c needs triage for user 1, not for user 2; a needs it for neither (dismissed for 2, scored for 1)
    assert [r["id"] for r in db.jobs_needing("triage", criteria, 10, user_id=1)] == [s["c"]]
    assert [r["id"] for r in db.jobs_needing("triage", criteria, 10, user_id=2)] == [s["b"]]

    # stats and facets are per user; job counts are shared
    assert db.stats(user_id=1)["dismissed"] == 0 and db.stats(user_id=2)["dismissed"] == 1
    assert db.stats(user_id=1)["jobs"] == db.stats(user_id=2)["jobs"] == 3
    assert db.facets(user_id=2)["status"] == {"dismissed": 1, "new": 2}

    # the same decision by both users on the same job is two rows
    db.set_user_state(s["b"], "applied", user_id=1); db.set_user_state(s["b"], "archived", user_id=2)
    assert db.get_job(s["b"], criteria_hash=criteria, user_id=1)["status"] == "applied"
    assert db.get_job(s["b"], criteria_hash=criteria, user_id=2)["status"] == "archived"


def test_deleting_a_user_removes_only_their_rows(two_users, criteria):
    s = two_users
    db.set_user_state(s["a"], "applied", user_id=2)
    with db.connect() as conn:
        db.record_evaluation(conn, job_id=s["a"], run_id=None, stage="triage", criteria_hash=criteria, score=1, verdict="reject", model="m", user_id=2)
        conn.execute("DELETE FROM users WHERE id = 2")
        assert conn.execute("SELECT COUNT(*) FROM user_state WHERE user_id = 2").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evaluations WHERE user_id = 2").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evaluations WHERE user_id = 1").fetchone()[0] == 3   # user 1's untouched
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3                              # jobs are shared, never cascade
