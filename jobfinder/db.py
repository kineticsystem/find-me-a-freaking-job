"""SQLite persistence.

One connection per operation, WAL enabled: the scheduler thread and the API
threads both touch this, and short-lived connections keep that trivially safe.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import settings
from .models import NormalizedJob, utcnow

log = logging.getLogger(__name__)

SCHEMA = """
-- Users. Until login exists there is exactly one, the default user (id 1),
-- created by init_db. Everything per-user hangs off this id from now on.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT,                        -- unique (index below); NULL until the account is set up
    password_hash TEXT,                        -- Argon2id hash, never the password; NULL = cannot log in
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
-- idx_users_email (unique) is created in init_db, after the migrations that add the column.

-- Who each user is: the CV (the uploaded file and the text pulled out of
-- it), their notes, and the model's digest of both. One row per user; in
-- the database rather than files so a backup of jobs.db is everything.
CREATE TABLE IF NOT EXISTS user_profile (
    user_id           INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    cv_name           TEXT,
    cv_data           BLOB,
    cv_text           TEXT NOT NULL DEFAULT '',
    cv_updated_at     TEXT,
    notes             TEXT NOT NULL DEFAULT '',
    notes_updated_at  TEXT,
    digest            TEXT,                     -- ProfileDigest JSON
    digest_hash       TEXT,                     -- source hash it was made from; stale when it differs
    digest_updated_at TEXT
);

-- Bearer tokens. Only the SHA-256 of the token is stored; the token itself is
-- shown once at login and never kept, so a stolen database yields no usable
-- token. One row per device or script; deleting a row logs that one out.
CREATE TABLE IF NOT EXISTS api_tokens (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash   TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    expires_at   TEXT,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,
    fingerprint   TEXT NOT NULL UNIQUE,
    source_id     TEXT NOT NULL,
    company       TEXT NOT NULL,
    title         TEXT NOT NULL,
    location      TEXT,
    remote_type   TEXT,
    url           TEXT,
    apply_url     TEXT,
    description   TEXT,
    salary_raw    TEXT,
    posted_at     TEXT,
    tags          TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    seen_count    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen DESC);

-- One row per (job, criteria) evaluation. History is kept so you can see how a
-- job scored under an older set of preferences.
CREATE TABLE IF NOT EXISTS evaluations (
    id             INTEGER PRIMARY KEY,
    job_id         INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    user_id        INTEGER NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
    run_id         INTEGER REFERENCES runs(id),
    stage          TEXT NOT NULL,           -- 'triage' | 'deepdive'
    criteria_hash  TEXT NOT NULL,
    score          INTEGER NOT NULL,
    verdict        TEXT NOT NULL,
    eligible       INTEGER,
    summary        TEXT,
    eligibility    TEXT,
    salary         TEXT,
    tech_stack     TEXT,
    concerns       TEXT,
    rationale      TEXT,
    model          TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_job ON evaluations(job_id, stage);
CREATE INDEX IF NOT EXISTS idx_eval_criteria ON evaluations(criteria_hash);
-- idx_eval_user is created in init_db, after the user_id migration has run.

-- Your actions. Deliberately separate from evaluations so a re-run never
-- overwrites what you decided.
CREATE TABLE IF NOT EXISTS user_state (
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'new',   -- new|shortlisted|applied|declined|dismissed|archived
    notes      TEXT,
    reason     TEXT,                          -- why it was dismissed; fed back into scoring
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, user_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,               -- running|ok|error
    stats       TEXT,
    error       TEXT
);

-- Sources: seeded from config/sources.yaml, then grown by the discovery layer.
CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    config        TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    origin        TEXT NOT NULL DEFAULT 'config',  -- config|discovered
    added_at      TEXT NOT NULL,
    last_run_at   TEXT,
    last_ok_at    TEXT,
    last_error    TEXT,
    jobs_found    INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0
);

-- Who follows a source. The registry row (what a source is, its health) is
-- shared; whether it is fetched for you is this row. A source is fetched if
-- anyone follows it, and its postings are shown to and scored for its
-- followers only.
CREATE TABLE IF NOT EXISTS user_sources (
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    enabled   INTEGER NOT NULL DEFAULT 1,
    added_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, source_id)
);

-- Every source a posting was seen from (the same job on two boards is one
-- row in jobs and two here). Visibility is: one of these is followed.
CREATE TABLE IF NOT EXISTS job_sources (
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_id  TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (job_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_job_sources_source ON job_sources(source_id);

-- One preferences document per user: the whole Preferences model as JSON,
-- validated on write and on read. A document rather than tables because
-- nothing ever queries inside it; it is loaded whole, handed to the model,
-- and hashed for the criteria. schema_version lets a later change to the
-- model migrate stored documents lazily on read.
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id        INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data           TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    updated_at     TEXT NOT NULL
);

-- Cache so the same search result URL is not re-inspected every two hours.
CREATE TABLE IF NOT EXISTS discovery_log (
    key        TEXT PRIMARY KEY,
    kind       TEXT,
    detail     TEXT,
    created_at TEXT NOT NULL
);
"""


def db_path() -> Path:
    p = settings().paths.resolve("db")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after the first release; applied to databases that predate them.
MIGRATIONS = [
    ("user_state", "reason", "ALTER TABLE user_state ADD COLUMN reason TEXT"),
    ("users", "email", "ALTER TABLE users ADD COLUMN email TEXT"),
    ("users", "password_hash", "ALTER TABLE users ADD COLUMN password_hash TEXT"),
    ("users", "is_admin", "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0"),
]


DEFAULT_USER_ID = 1


def _migrate_per_user(conn: sqlite3.Connection) -> None:
    """Scores and decisions became per user. SQLite can neither change a
    primary key in place nor add a foreign-key column with a default, so an
    old single-user table is rebuilt, every existing row becoming the
    default user's. Nothing is lost."""
    if "user_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(evaluations)")}:
        conn.executescript("""
            CREATE TABLE evaluations_v2 (
                id             INTEGER PRIMARY KEY,
                job_id         INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                user_id        INTEGER NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
                run_id         INTEGER REFERENCES runs(id),
                stage          TEXT NOT NULL,
                criteria_hash  TEXT NOT NULL,
                score          INTEGER NOT NULL,
                verdict        TEXT NOT NULL,
                eligible       INTEGER,
                summary        TEXT,
                eligibility    TEXT,
                salary         TEXT,
                tech_stack     TEXT,
                concerns       TEXT,
                rationale      TEXT,
                model          TEXT,
                created_at     TEXT NOT NULL
            );
            INSERT INTO evaluations_v2 (id, job_id, user_id, run_id, stage, criteria_hash, score, verdict, eligible,
                                        summary, eligibility, salary, tech_stack, concerns, rationale, model, created_at)
                SELECT id, job_id, 1, run_id, stage, criteria_hash, score, verdict, eligible,
                       summary, eligibility, salary, tech_stack, concerns, rationale, model, created_at FROM evaluations;
            DROP TABLE evaluations;
            ALTER TABLE evaluations_v2 RENAME TO evaluations;
            CREATE INDEX IF NOT EXISTS idx_eval_job ON evaluations(job_id, stage);
            CREATE INDEX IF NOT EXISTS idx_eval_criteria ON evaluations(criteria_hash);
        """)
    if "user_id" in {r["name"] for r in conn.execute("PRAGMA table_info(user_state)")}:
        return
    conn.executescript("""
        CREATE TABLE user_state_v2 (
            job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            user_id    INTEGER NOT NULL DEFAULT 1 REFERENCES users(id) ON DELETE CASCADE,
            status     TEXT NOT NULL DEFAULT 'new',
            notes      TEXT,
            reason     TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (job_id, user_id)
        );
        INSERT INTO user_state_v2 (job_id, user_id, status, notes, reason, updated_at)
            SELECT job_id, 1, status, notes, reason, updated_at FROM user_state;
        DROP TABLE user_state;
        ALTER TABLE user_state_v2 RENAME TO user_state;
    """)


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO users (id, name, created_at) VALUES (?, 'default', ?)",
            (DEFAULT_USER_ID, utcnow()),
        )
        for table, column, ddl in MIGRATIONS:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                conn.execute(ddl)
        _migrate_per_user(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_user ON evaluations(user_id, job_id, stage)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")
        # The pre-login default user owns all existing data; it becomes the admin.
        conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (DEFAULT_USER_ID,))
        _migrate_sources_per_user(conn)


def _migrate_sources_per_user(conn: sqlite3.Connection) -> None:
    """A database from before following existed: every user follows every
    source as it was (on or off), and every job is attributed to the source
    it was first seen from."""
    now = utcnow()
    if conn.execute("SELECT 1 FROM sources LIMIT 1").fetchone() and not conn.execute("SELECT 1 FROM user_sources LIMIT 1").fetchone():
        conn.execute(
            """INSERT OR IGNORE INTO user_sources (user_id, source_id, enabled, added_at)
               SELECT u.id, s.id, s.enabled, ? FROM users u CROSS JOIN sources s""", (now,))
        log.info("sources: every user now follows the %d existing sources", conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
    if conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchone() and not conn.execute("SELECT 1 FROM job_sources LIMIT 1").fetchone():
        conn.execute("INSERT OR IGNORE INTO job_sources (job_id, source_id, first_seen) SELECT id, source_id, first_seen FROM jobs")


# A job this user can see: seen from a source they follow, or one they have
# already made a decision on (unfollowing a board must not hide a shortlist).
# `u` is the user_state alias of the enclosing query; the user id is bound once.
_VISIBLE = """(EXISTS (SELECT 1 FROM job_sources js JOIN user_sources us
                        ON us.source_id = js.source_id AND us.user_id = ? AND us.enabled = 1
                       WHERE js.job_id = j.id)
               OR u.status IS NOT NULL)"""


# --------------------------------------------------------------------------
# user profile: CV, notes, digest
# --------------------------------------------------------------------------
def get_profile(user_id: int) -> dict[str, Any]:
    """Everything but the CV bytes. Always returns a dict (empty fields when nothing is stored)."""
    with connect() as conn:
        row = conn.execute(
            """SELECT cv_name, length(cv_data) AS cv_bytes, cv_text, cv_updated_at, notes, notes_updated_at,
                      digest, digest_hash, digest_updated_at FROM user_profile WHERE user_id = ?""", (user_id,)
        ).fetchone()
    return dict(row) if row else {"cv_name": None, "cv_bytes": None, "cv_text": "", "cv_updated_at": None,
                                  "notes": "", "notes_updated_at": None, "digest": None, "digest_hash": None,
                                  "digest_updated_at": None}


def get_cv_blob(user_id: int) -> tuple[str, bytes] | None:
    with connect() as conn:
        row = conn.execute("SELECT cv_name, cv_data FROM user_profile WHERE user_id = ?", (user_id,)).fetchone()
    return (row["cv_name"], bytes(row["cv_data"])) if row and row["cv_data"] else None


def _ensure_profile_row(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO user_profile (user_id) VALUES (?)", (user_id,))


def save_cv(user_id: int, name: str, data: bytes, text: str) -> None:
    with connect() as conn:
        _ensure_profile_row(conn, user_id)
        conn.execute("UPDATE user_profile SET cv_name = ?, cv_data = ?, cv_text = ?, cv_updated_at = ? WHERE user_id = ?",
                     (name, data, text, utcnow(), user_id))


def save_notes(user_id: int, text: str) -> None:
    with connect() as conn:
        _ensure_profile_row(conn, user_id)
        conn.execute("UPDATE user_profile SET notes = ?, notes_updated_at = ? WHERE user_id = ?", (text, utcnow(), user_id))


def save_digest(user_id: int, source_hash: str, digest_json: str) -> None:
    with connect() as conn:
        _ensure_profile_row(conn, user_id)
        conn.execute("UPDATE user_profile SET digest = ?, digest_hash = ?, digest_updated_at = ? WHERE user_id = ?",
                     (digest_json, source_hash, utcnow(), user_id))


def all_preferences_docs() -> list[tuple[int, str]]:
    with connect() as conn:
        return [(int(r["user_id"]), r["data"]) for r in conn.execute("SELECT user_id, data FROM user_preferences")]


# --------------------------------------------------------------------------
# users and tokens
# --------------------------------------------------------------------------
def _user(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    d = dict(row)
    d["is_admin"] = bool(d.get("is_admin"))
    d["can_login"] = bool(d.get("password_hash"))
    d.pop("password_hash", None)
    return d


def get_user(user_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        return _user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def get_user_by_email(email: str) -> dict[str, Any] | None:
    with connect() as conn:
        return _user(conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone())


def get_password_hash(email: str | None = None, *, user_id: int | None = None) -> tuple[int, str] | None:
    with connect() as conn:
        if user_id is not None:
            row = conn.execute("SELECT id, password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        else:
            row = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", ((email or "").strip().lower(),)).fetchone()
    return (int(row["id"]), row["password_hash"]) if row and row["password_hash"] else None


def list_users() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT u.*, (SELECT COUNT(*) FROM api_tokens t WHERE t.user_id = u.id) AS tokens
               FROM users u ORDER BY u.id"""
        ).fetchall()
    return [_user(r) for r in rows]


def any_user_can_login() -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM users WHERE password_hash IS NOT NULL LIMIT 1").fetchone() is not None


def create_user(email: str, password_hash: str, *, name: str = "", is_admin: bool = False) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (name, email, password_hash, is_admin, created_at) VALUES (?,?,?,?,?)",
            (name or email.split("@")[0], email.strip().lower(), password_hash, int(is_admin), utcnow()),
        )
        uid = int(cur.lastrowid)
        conn.execute("""INSERT OR IGNORE INTO user_sources (user_id, source_id, enabled, added_at)
                        SELECT ?, id, enabled, ? FROM sources WHERE origin = 'config'""", (uid, utcnow()))
        return uid


def claim_user(user_id: int, email: str, password_hash: str, *, is_admin: bool) -> None:
    """Give an existing account (the pre-login default user) an email and a
    password, so its data becomes somebody's."""
    with connect() as conn:
        conn.execute("UPDATE users SET email = ?, password_hash = ?, is_admin = ?, name = ? WHERE id = ?",
                     (email.strip().lower(), password_hash, int(is_admin), email.split("@")[0], user_id))


def set_password_hash(user_id: int, password_hash: str) -> bool:
    with connect() as conn:
        return conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id)).rowcount > 0


def delete_user(user_id: int) -> bool:
    """Removes the user and, by cascade, their tokens, decisions, scores and
    preferences. Jobs are shared and stay."""
    with connect() as conn:
        return conn.execute("DELETE FROM users WHERE id = ?", (user_id,)).rowcount > 0


def create_token(user_id: int, token_hash: str, name: str = "", expires_at: str | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO api_tokens (user_id, token_hash, name, created_at, expires_at) VALUES (?,?,?,?,?)",
            (user_id, token_hash, name, utcnow(), expires_at),
        )
        return int(cur.lastrowid)


def resolve_token(token_hash: str) -> dict[str, Any] | None:
    """The user a valid token belongs to, or None. Touches last_used_at."""
    with connect() as conn:
        row = conn.execute(
            """SELECT u.*, t.id AS token_id, t.expires_at FROM api_tokens t JOIN users u ON u.id = t.user_id
               WHERE t.token_hash = ?""", (token_hash,)
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] and row["expires_at"] < utcnow():
            conn.execute("DELETE FROM api_tokens WHERE id = ?", (row["token_id"],))
            return None
        conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (utcnow(), row["token_id"]))
        user = _user(row)
        assert user is not None
        user["token_id"] = int(row["token_id"])
        return user


def delete_token(token_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))


def delete_user_tokens(user_id: int, keep: int | None = None) -> int:
    with connect() as conn:
        return conn.execute("DELETE FROM api_tokens WHERE user_id = ? AND id IS NOT ?", (user_id, keep)).rowcount


# --------------------------------------------------------------------------
# per-user documents
# --------------------------------------------------------------------------
PREFERENCES_SCHEMA_VERSION = 1


def get_preferences_doc(user_id: int) -> dict[str, Any] | None:
    """The stored document, or None if the user has never saved one."""
    with connect() as conn:
        row = conn.execute("SELECT data, schema_version FROM user_preferences WHERE user_id = ?", (user_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def save_preferences_doc(user_id: int, data: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO users (id, name, created_at) VALUES (?, ?, ?)", (user_id, f"user{user_id}", utcnow()))
        conn.execute(
            """INSERT INTO user_preferences (user_id, data, schema_version, updated_at) VALUES (?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET data = excluded.data,
                 schema_version = excluded.schema_version, updated_at = excluded.updated_at""",
            (user_id, json.dumps(data, ensure_ascii=False), PREFERENCES_SCHEMA_VERSION, utcnow()),
        )


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------
def upsert_job(conn: sqlite3.Connection, job: NormalizedJob) -> tuple[int, bool]:
    """Insert or refresh a posting. Returns (job_id, is_new)."""
    now = utcnow()
    row = conn.execute(
        "SELECT id FROM jobs WHERE fingerprint = ?", (job.fingerprint,)
    ).fetchone()
    if row:
        conn.execute(
            """UPDATE jobs SET last_seen = ?, seen_count = seen_count + 1,
                   description = CASE WHEN length(?) > length(description)
                                      THEN ? ELSE description END,
                   salary_raw = COALESCE(NULLIF(salary_raw, ''), ?)
               WHERE id = ?""",
            (now, job.description, job.description, job.salary_raw, row["id"]),
        )
        add_job_source(conn, int(row["id"]), job.source_id, now)
        return int(row["id"]), False

    cur = conn.execute(
        """INSERT INTO jobs (fingerprint, source_id, company, title, location,
                             remote_type, url, apply_url, description, salary_raw,
                             posted_at, tags, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            job.fingerprint, job.source_id, job.company, job.title, job.location,
            job.remote_type, job.url, job.apply_url, job.description, job.salary_raw,
            job.posted_at, json.dumps(job.tags), now, now,
        ),
    )
    job_id = int(cur.lastrowid)
    add_job_source(conn, job_id, job.source_id, now)
    return job_id, True


def add_job_source(conn: sqlite3.Connection, job_id: int, source_id: str, when: str | None = None) -> None:
    conn.execute("INSERT OR IGNORE INTO job_sources (job_id, source_id, first_seen) VALUES (?,?,?)",
                 (job_id, source_id, when or utcnow()))


def jobs_needing(stage: str, criteria_hash: str, limit: int = -1, user_id: int = DEFAULT_USER_ID) -> list[sqlite3.Row]:
    """Jobs with no evaluation for this user and stage under the current
    criteria; all of them unless a limit is given (-1 is SQLite's "none")."""
    with connect() as conn:
        return conn.execute(
            f"""SELECT j.* FROM jobs j
               LEFT JOIN user_state u ON u.job_id = j.id AND u.user_id = ?
               WHERE COALESCE(u.status, 'new') NOT IN ('dismissed', 'archived')
                 AND {_VISIBLE}
                 AND NOT EXISTS (
                       SELECT 1 FROM evaluations e
                       WHERE e.job_id = j.id AND e.user_id = ? AND e.stage = ?
                         AND e.criteria_hash = ?)
               ORDER BY j.first_seen DESC
               LIMIT ?""",
            (user_id, user_id, user_id, stage, criteria_hash, limit),
        ).fetchall()


def deepdive_candidates(criteria_hash: str, min_score: int, limit: int, user_id: int = DEFAULT_USER_ID) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            f"""SELECT j.*, e.score AS triage_score FROM jobs j
               JOIN evaluations e ON e.job_id = j.id AND e.user_id = ?
                    AND e.stage = 'triage' AND e.criteria_hash = ?
               LEFT JOIN user_state u ON u.job_id = j.id AND u.user_id = ?
               WHERE e.score >= ?
                 AND COALESCE(u.status, 'new') NOT IN ('dismissed', 'archived')
                 AND {_VISIBLE}
                 AND NOT EXISTS (
                       SELECT 1 FROM evaluations d
                       WHERE d.job_id = j.id AND d.user_id = ? AND d.stage = 'deepdive'
                         AND d.criteria_hash = ?)
               ORDER BY e.score DESC
               LIMIT ?""",
            (user_id, criteria_hash, user_id, min_score, user_id, user_id, criteria_hash, limit),
        ).fetchall()


def record_evaluation(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    run_id: int | None,
    stage: str,
    criteria_hash: str,
    score: int,
    verdict: str,
    model: str,
    eligible: bool | None = None,
    summary: str = "",
    eligibility: str = "",
    salary: str = "",
    tech_stack: Iterable[str] = (),
    concerns: Iterable[str] = (),
    rationale: str = "",
    user_id: int = DEFAULT_USER_ID,
) -> None:
    conn.execute(
        """INSERT INTO evaluations (job_id, user_id, run_id, stage, criteria_hash, score,
               verdict, eligible, summary, eligibility, salary, tech_stack,
               concerns, rationale, model, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            job_id, user_id, run_id, stage, criteria_hash, score, verdict,
            None if eligible is None else int(eligible), summary, eligibility,
            salary, json.dumps(list(tech_stack)), json.dumps(list(concerns)),
            rationale, model, utcnow(),
        ),
    )


def list_jobs(
    *, min_score: int = 0, status: str | None = None, source: str | None = None,
    query: str | None = None, remote: str | None = None,
    hidden: bool = False, sort: str = "score",
    limit: int = 100, offset: int = 0, criteria_hash: str | None = None,
    user_id: int = DEFAULT_USER_ID,
) -> tuple[list[dict[str, Any]], int]:
    """Jobs joined with their best current evaluation. Returns (page, total).

    `status=None` means the active statuses -- new, shortlisted, applied.
    `hidden=True` flips that: only archived, dismissed and declined. Pass an explicit
    status to see exactly that one.
    """
    from .pipeline.criteria import current_criteria_hash

    ch = criteria_hash or current_criteria_hash(user_id)
    sql = f"""
        -- A deep dive supersedes the triage score it was derived from; the
        -- triage score stands in until one exists. Scores belong to the
        -- criteria (CV + preferences) they were made under; when those have
        -- changed since, the most recent score under the OLD criteria is
        -- shown, flagged stale, until the next scan replaces it -- rather
        -- than blanking the whole list until then.
        WITH best AS (
            SELECT job_id,
                   MAX(CASE WHEN stage='triage' THEN score END) AS triage_score,
                   MAX(CASE WHEN stage='deepdive' THEN id END) AS dd_id
            FROM evaluations WHERE user_id = ? AND criteria_hash = ? GROUP BY job_id
        ),
        stale AS (
            SELECT job_id, MAX(id) AS eid FROM evaluations WHERE user_id = ? AND criteria_hash != ? GROUP BY job_id
        )
        SELECT j.*, COALESCE(u.status,'new') AS status, u.notes, u.reason,
               COALESCE(d.score, b.triage_score, o.score) AS score,
               CASE WHEN d.score IS NULL AND b.triage_score IS NULL AND o.score IS NOT NULL THEN 1 ELSE 0 END AS score_stale,
               COALESCE(d.verdict, o.verdict) AS verdict, COALESCE(d.summary, o.summary) AS summary,
               COALESCE(d.eligibility, o.eligibility) AS eligibility, COALESCE(d.eligible, o.eligible) AS eligible,
               COALESCE(d.salary, o.salary) AS salary, COALESCE(d.tech_stack, o.tech_stack) AS tech_stack,
               COALESCE(d.concerns, o.concerns) AS concerns, COALESCE(d.rationale, o.rationale) AS rationale
        FROM jobs j
        LEFT JOIN best b ON b.job_id = j.id
        LEFT JOIN evaluations d ON d.id = b.dd_id
        LEFT JOIN stale s ON s.job_id = j.id AND b.job_id IS NULL
        LEFT JOIN evaluations o ON o.id = s.eid
        LEFT JOIN user_state u ON u.job_id = j.id AND u.user_id = ?
        -- unevaluated jobs count as 0, so min_score=0 shows everything
        WHERE COALESCE(d.score, b.triage_score, o.score, 0) >= ?
          AND {_VISIBLE}
    """
    params: list[Any] = [user_id, ch, user_id, ch, user_id, min_score, user_id]
    if status:
        sql += " AND COALESCE(u.status,'new') = ?"
        params.append(status)
    elif hidden:
        sql += " AND COALESCE(u.status,'new') IN ('archived', 'dismissed', 'declined')"
    else:
        sql += " AND COALESCE(u.status,'new') NOT IN ('archived', 'dismissed', 'declined')"
    if source:
        sql += " AND EXISTS (SELECT 1 FROM job_sources js WHERE js.job_id = j.id AND js.source_id = ?)"
        params.append(source)
    if remote:
        sql += " AND j.remote_type = ?"
        params.append(remote)
    if query:
        sql += " AND (j.title LIKE ? OR j.company LIKE ? OR j.location LIKE ? OR j.description LIKE ?)"
        params += [f"%{query}%"] * 4

    orders = {
        "score": "COALESCE(d.score, b.triage_score, o.score) DESC NULLS LAST, j.first_seen DESC",
        "newest": "j.first_seen DESC, COALESCE(d.score, b.triage_score, o.score) DESC NULLS LAST",
        "company": "j.company COLLATE NOCASE ASC, j.title COLLATE NOCASE ASC",
    }
    order_by = orders.get(sort, orders["score"])

    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM ({sql})", params
        ).fetchone()[0]
        rows = conn.execute(
            f"{sql} ORDER BY {order_by} LIMIT ? OFFSET ?", params + [limit, offset]
        ).fetchall()
        return [dict(r) for r in rows], int(total)


def get_job(job_id: int, criteria_hash: str | None = None, user_id: int = DEFAULT_USER_ID) -> dict[str, Any] | None:
    from .pipeline.criteria import current_criteria_hash

    ch = criteria_hash or current_criteria_hash(user_id)
    with connect() as conn:
        row = conn.execute(
            """SELECT j.*, COALESCE(u.status,'new') AS status, u.notes, u.reason
               FROM jobs j LEFT JOIN user_state u ON u.job_id = j.id AND u.user_id = ?
               WHERE j.id = ?""",
            (user_id, job_id),
        ).fetchone()
        if not row:
            return None
        job = dict(row)
        job["evaluations"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM evaluations WHERE job_id = ? AND user_id = ? ORDER BY id DESC", (job_id, user_id)
            ).fetchall()
        ]
        job["current_criteria"] = ch
        return job


def set_user_state(job_id: int, status: str, notes: str | None = None,
                   reason: str | None = None, user_id: int = DEFAULT_USER_ID) -> bool:
    """Record a decision. `reason` is kept only while the job is dismissed:
    restoring it clears the reason so it stops influencing scores."""
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
            return False
        if status != "dismissed":
            reason = None
        conn.execute(
            """INSERT INTO user_state (job_id, user_id, status, notes, reason, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(job_id, user_id) DO UPDATE SET
                 status = excluded.status,
                 notes = COALESCE(excluded.notes, user_state.notes),
                 reason = CASE WHEN excluded.status = 'dismissed'
                               THEN COALESCE(excluded.reason, user_state.reason)
                               ELSE NULL END,
                 updated_at = excluded.updated_at""",
            (job_id, user_id, status, notes, reason, utcnow()),
        )
        return True


def recent_rejections(limit: int = 20, user_id: int = DEFAULT_USER_ID) -> list[dict[str, Any]]:
    """This user's dismissed jobs with a stated reason, newest first: the
    few-shot negative signal the prompts carry."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT j.company, j.title, j.location, j.remote_type, u.reason, u.updated_at
               FROM user_state u JOIN jobs j ON j.id = u.job_id
               WHERE u.user_id = ? AND u.status = 'dismissed' AND u.reason IS NOT NULL AND u.reason != ''
               ORDER BY u.updated_at DESC, u.job_id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_job(job_id: int) -> bool:
    """Permanently remove a job; evaluations and user_state cascade."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cur.rowcount > 0


def archive_jobs(*, older_than_days: int | None = None, ids: Iterable[int] | None = None,
                 only_status: str = "new", user_id: int = DEFAULT_USER_ID) -> int:
    """Bulk archive. By age (jobs not seen in N days) and/or by explicit id.

    Only jobs in `only_status` are touched by the age rule, so a shortlist is
    never silently archived from under you; explicit ids always archive.
    Returns the number of jobs whose status changed.
    """
    now = utcnow()
    changed = 0
    with connect() as conn:
        if older_than_days is not None:
            cur = conn.execute(
                f"""INSERT INTO user_state (job_id, user_id, status, updated_at)
                   SELECT j.id, ?, 'archived', ? FROM jobs j
                   LEFT JOIN user_state u ON u.job_id = j.id AND u.user_id = ?
                   WHERE COALESCE(u.status, 'new') = ?
                     AND {_VISIBLE}
                     AND datetime(j.last_seen) <= datetime('now', ?)
                   ON CONFLICT(job_id, user_id) DO UPDATE SET
                     status = 'archived', updated_at = excluded.updated_at
                   WHERE user_state.status != 'archived'""",
                (user_id, now, user_id, only_status, user_id, f"-{int(older_than_days)} days"),
            )
            changed += cur.rowcount
        for job_id in ids or []:
            cur = conn.execute(
                """INSERT INTO user_state (job_id, user_id, status, updated_at)
                   SELECT id, ?, 'archived', ? FROM jobs WHERE id = ?
                   ON CONFLICT(job_id, user_id) DO UPDATE SET
                     status = 'archived', updated_at = excluded.updated_at
                   WHERE user_state.status != 'archived'""",
                (user_id, now, int(job_id)),
            )
            changed += cur.rowcount
    return changed


def facets(user_id: int = DEFAULT_USER_ID) -> dict[str, Any]:
    """Distinct values the UI offers as filters, with counts."""
    with connect() as conn:
        by_status = {
            r["status"]: r["n"] for r in conn.execute(
                f"""SELECT COALESCE(u.status,'new') AS status, COUNT(*) AS n
                   FROM jobs j LEFT JOIN user_state u ON u.job_id = j.id AND u.user_id = ?
                   WHERE {_VISIBLE} GROUP BY 1 ORDER BY 1""", (user_id, user_id)
            )
        }
        by_source = {
            r["source_id"]: r["n"] for r in conn.execute(
                """SELECT js.source_id, COUNT(DISTINCT js.job_id) AS n FROM job_sources js
                   JOIN user_sources us ON us.source_id = js.source_id AND us.user_id = ? AND us.enabled = 1
                   GROUP BY 1 ORDER BY 2 DESC""", (user_id,)
            )
        }
        by_remote = {
            r["remote_type"]: r["n"] for r in conn.execute(
                f"""SELECT COALESCE(j.remote_type,'unknown') AS remote_type, COUNT(*) AS n
                   FROM jobs j LEFT JOIN user_state u ON u.job_id = j.id AND u.user_id = ?
                   WHERE {_VISIBLE} GROUP BY 1""", (user_id, user_id)
            )
        }
    return {"status": by_status, "source": by_source, "remote": by_remote}


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------
def start_run() -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (started_at, status) VALUES (?, 'running')", (utcnow(),)
        )
        return int(cur.lastrowid)


def finish_run(run_id: int, status: str, stats: dict[str, Any], error: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET finished_at=?, status=?, stats=?, error=? WHERE id=?",
            (utcnow(), status, json.dumps(stats), error, run_id),
        )


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["stats"] = json.loads(d["stats"]) if d["stats"] else {}
        out.append(d)
    return out


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------
def upsert_source(cfg: dict[str, Any], origin: str = "config", followers: Iterable[int] | None = None) -> bool:
    """Register a source. Returns True if it was newly added. `followers`
    start following it (config sources: everyone; None = nobody yet)."""
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM sources WHERE id = ?", (cfg["id"],)
        ).fetchone()
        if existing:
            # The YAML stays authoritative for WHAT a config source is, but not
            # for whether it is on: that is decided in the UI, and a re-sync on
            # every run must not switch a source back on. `enabled` in the YAML
            # therefore only applies when the row is first created.
            if origin in ("config", "keyword"):     # keyword rows are regenerated each scan
                conn.execute(
                    "UPDATE sources SET type=?, config=? WHERE id=?",
                    (cfg["type"], json.dumps(cfg), cfg["id"]),
                )
            return False
        conn.execute(
            """INSERT INTO sources (id, type, config, enabled, origin, added_at)
               VALUES (?,?,?,?,?,?)""",
            (cfg["id"], cfg["type"], json.dumps(cfg),
             int(cfg.get("enabled", True)), origin, utcnow()),
        )
        if origin == "config":
            followers = [r["id"] for r in conn.execute("SELECT id FROM users")]
        for uid in followers or []:
            conn.execute("INSERT OR IGNORE INTO user_sources (user_id, source_id, enabled, added_at) VALUES (?,?,?,?)",
                         (uid, cfg["id"], int(cfg.get("enabled", True)), utcnow()))
        return True


def follow_source(user_id: int, source_id: str, enabled: bool = True) -> bool:
    """Follow (or switch off, for this user only). Following a board that had
    been switched off after failures gives it another chance."""
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone():
            return False
        conn.execute(
            """INSERT INTO user_sources (user_id, source_id, enabled, added_at) VALUES (?,?,?,?)
               ON CONFLICT(user_id, source_id) DO UPDATE SET enabled = excluded.enabled""",
            (user_id, source_id, int(enabled), utcnow()))
        if enabled:
            conn.execute("UPDATE sources SET enabled = 1, fail_count = 0 WHERE id = ?", (source_id,))
        return True


def follows(user_id: int, source_id: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM user_sources WHERE user_id = ? AND source_id = ? AND enabled = 1",
                            (user_id, source_id)).fetchone() is not None


def source_followers(source_id: str) -> list[int]:
    """Who follows it (switched on). A discovered board inherits these."""
    with connect() as conn:
        return [int(r["user_id"]) for r in conn.execute(
            "SELECT user_id FROM user_sources WHERE source_id = ? AND enabled = 1 ORDER BY user_id", (source_id,))]


def follow_config_sources(user_id: int) -> int:
    """A new account follows the seed list, as everyone does by default."""
    with connect() as conn:
        return conn.execute(
            """INSERT OR IGNORE INTO user_sources (user_id, source_id, enabled, added_at)
               SELECT ?, id, enabled, ? FROM sources WHERE origin = 'config'""", (user_id, utcnow())).rowcount


def active_sources() -> list[dict[str, Any]]:
    """What the shared fetch runs: on, not failing, followed by someone.
    Keyword sources are fetched per user by the keyword step, not here."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM sources s WHERE enabled = 1 AND fail_count < 5 AND origin != 'keyword'
                 AND EXISTS (SELECT 1 FROM user_sources us WHERE us.source_id = s.id AND us.enabled = 1)
               ORDER BY id"""
        ).fetchall()
    return [json.loads(r["config"]) | {"_origin": r["origin"]} for r in rows]


def list_sources(user_id: int | None = None) -> list[dict[str, Any]]:
    """The whole registry, or one user's list: exactly the rows they have a
    follow row for (on or off). What others added or had discovered is not
    theirs to see; the registry row is shared underneath so a board two
    people add is still fetched once."""
    with connect() as conn:
        if user_id is None:
            return [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY id")]
        rows = conn.execute(
            """SELECT s.*, me.enabled AS following
               FROM sources s JOIN user_sources me ON me.source_id = s.id AND me.user_id = ?
               ORDER BY s.id""", (user_id,)).fetchall()
        return [dict(r) for r in rows]


def in_list(user_id: int, source_id: str) -> bool:
    """Whether the source is in this user's list at all (on or off)."""
    with connect() as conn:
        return conn.execute("SELECT 1 FROM user_sources WHERE user_id = ? AND source_id = ?",
                            (user_id, source_id)).fetchone() is not None


def record_source_result(source_id: str, found: int, error: str = "") -> None:
    with connect() as conn:
        if error:
            conn.execute(
                """UPDATE sources SET last_run_at=?, last_error=?,
                       fail_count = fail_count + 1 WHERE id=?""",
                (utcnow(), error[:500], source_id),
            )
        else:
            conn.execute(
                """UPDATE sources SET last_run_at=?, last_ok_at=?, last_error='',
                       fail_count = 0, jobs_found = jobs_found + ? WHERE id=?""",
                (utcnow(), utcnow(), found, source_id),
            )


def set_source_enabled(source_id: str, enabled: bool) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE sources SET enabled=?, fail_count=0 WHERE id=?",
            (int(enabled), source_id),
        )
        return cur.rowcount > 0


def get_source(source_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return dict(row) if row else None


def delete_source(source_id: str) -> bool:
    """Remove a registry row; its jobs stay. Config-origin sources cannot be
    deleted (the YAML would recreate them) -- switch those off instead."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM sources WHERE id = ? AND origin != 'config'", (source_id,))
        return cur.rowcount > 0


def prune_keyword_sources(user_id: int, keep: Iterable[str]) -> int:
    """Drop this user's keyword searches that the current CV no longer
    produces (a changed keyword or region). Rows another user also has are
    only unfollowed."""
    keep = set(keep)
    with connect() as conn:
        rows = [r["source_id"] for r in conn.execute(
            """SELECT us.source_id FROM user_sources us JOIN sources s ON s.id = us.source_id
               WHERE us.user_id = ? AND s.origin = 'keyword'""", (user_id,))]
        stale = [sid for sid in rows if sid not in keep]
        for sid in stale:
            conn.execute("DELETE FROM user_sources WHERE user_id = ? AND source_id = ?", (user_id, sid))
            if not conn.execute("SELECT 1 FROM user_sources WHERE source_id = ?", (sid,)).fetchone():
                conn.execute("DELETE FROM sources WHERE id = ?", (sid,))
        return len(stale)


def unfollow_source(user_id: int, source_id: str) -> str | None:
    """Take a source out of one user's list. The registry row stays while
    anyone else has it; once nobody does it is dropped too (it would never
    be fetched again). Returns 'removed', 'dropped' (row gone as well), or
    None if it was not in the list."""
    with connect() as conn:
        if conn.execute("DELETE FROM user_sources WHERE user_id = ? AND source_id = ?", (user_id, source_id)).rowcount == 0:
            return None
        if conn.execute("SELECT 1 FROM user_sources WHERE source_id = ?", (source_id,)).fetchone():
            return "removed"
        conn.execute("DELETE FROM sources WHERE id = ? AND origin != 'config'", (source_id,))
        return "dropped"


def source_job_counts() -> dict[str, int]:
    with connect() as conn:
        return {r["source_id"]: r["n"] for r in conn.execute("SELECT source_id, COUNT(*) AS n FROM job_sources GROUP BY 1")}


# --------------------------------------------------------------------------
# discovery memo
# --------------------------------------------------------------------------
def seen_discovery(key: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM discovery_log WHERE key = ?", (key,)
        ).fetchone() is not None


def mark_discovery(key: str, kind: str, detail: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO discovery_log (key, kind, detail, created_at) VALUES (?,?,?,?)",
            (key, kind, detail[:500], utcnow()),
        )


# --------------------------------------------------------------------------
# destructive resets, driven from the settings panel
# --------------------------------------------------------------------------
def delete_all_jobs() -> dict[str, int]:
    """Every posting, its scores, your decisions on it, and the run history.
    Sources, discovery memory, and the files (CV, notes, preferences) stay,
    so the next scan starts the search over with the same setup."""
    with connect() as conn:
        counts = {
            "jobs": conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],
            "evaluations": conn.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0],
            "decisions": conn.execute("SELECT COUNT(*) FROM user_state WHERE status != 'new'").fetchone()[0],
            "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
        }
        conn.execute("DELETE FROM evaluations")
        conn.execute("DELETE FROM user_state")
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM runs")
        # HN comments were remembered so they are only structured once; with
        # their postings gone they must be eligible again.
        conn.execute("DELETE FROM discovery_log WHERE kind = 'hn_comment'")
        conn.execute("UPDATE sources SET jobs_found = 0, fail_count = 0, last_error = ''")
    with connect() as conn:
        conn.execute("VACUUM")
    return counts


def reset_everything() -> dict[str, int]:
    """delete_all_jobs plus the source registry and the discovery memory:
    a fresh database. The seed sources come back from config on the next
    start or scan."""
    counts = delete_all_jobs()
    with connect() as conn:
        counts["sources"] = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        conn.execute("DELETE FROM sources")
        conn.execute("DELETE FROM discovery_log")
        # users, their profiles (CV, notes) and preferences are not job data; they stay
    with connect() as conn:
        conn.execute("VACUUM")
    return counts


def stale_score_count(criteria_hash: str, user_id: int = DEFAULT_USER_ID) -> int:
    """This user's jobs whose only scores predate the current criteria."""
    with connect() as conn:
        return conn.execute(
            f"""SELECT COUNT(*) FROM jobs j LEFT JOIN user_state u ON u.job_id = j.id AND u.user_id = ?
               WHERE EXISTS (SELECT 1 FROM evaluations e WHERE e.job_id = j.id AND e.user_id = ?)
                 AND {_VISIBLE}
                 AND NOT EXISTS (SELECT 1 FROM evaluations e WHERE e.job_id = j.id AND e.user_id = ? AND e.criteria_hash = ?)""",
            (user_id, user_id, user_id, user_id, criteria_hash),
        ).fetchone()[0]


def stats(user_id: int = DEFAULT_USER_ID) -> dict[str, Any]:
    with connect() as conn:
        q = lambda s, *p: conn.execute(s, p).fetchone()[0]  # noqa: E731
        return {
            "jobs": q(f"SELECT COUNT(*) FROM jobs j LEFT JOIN user_state u ON u.job_id = j.id AND u.user_id = ? WHERE {_VISIBLE}", user_id, user_id),
            "evaluated": q("SELECT COUNT(DISTINCT job_id) FROM evaluations WHERE user_id = ?", user_id),
            "shortlisted": q("SELECT COUNT(*) FROM user_state WHERE user_id = ? AND status='shortlisted'", user_id),
            "applied": q("SELECT COUNT(*) FROM user_state WHERE user_id = ? AND status='applied'", user_id),
            "declined": q("SELECT COUNT(*) FROM user_state WHERE user_id = ? AND status='declined'", user_id),
            "dismissed": q("SELECT COUNT(*) FROM user_state WHERE user_id = ? AND status='dismissed'", user_id),
            "archived": q("SELECT COUNT(*) FROM user_state WHERE user_id = ? AND status='archived'", user_id),
            "sources": q("SELECT COUNT(*) FROM user_sources us JOIN sources s ON s.id = us.source_id WHERE us.user_id = ? AND us.enabled = 1 AND s.enabled = 1", user_id),
            "discovered_sources": q("SELECT COUNT(*) FROM sources WHERE origin='discovered'"),
            "runs": q("SELECT COUNT(*) FROM runs"),
        }
