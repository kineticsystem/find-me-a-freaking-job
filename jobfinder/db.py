"""SQLite persistence.

One connection per operation, WAL enabled: the scheduler thread and the API
threads both touch this, and short-lived connections keep that trivially safe.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import settings
from .models import NormalizedJob, utcnow

SCHEMA = """
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

-- Your actions. Deliberately separate from evaluations so a re-run never
-- overwrites what you decided.
CREATE TABLE IF NOT EXISTS user_state (
    job_id     INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'new',   -- new|shortlisted|applied|dismissed|archived
    notes      TEXT,
    reason     TEXT,                          -- why it was dismissed; fed back into scoring
    updated_at TEXT NOT NULL
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
]


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                conn.execute(ddl)


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
    conn.execute(
        "INSERT OR IGNORE INTO user_state (job_id, status, updated_at) VALUES (?, 'new', ?)",
        (job_id, now),
    )
    return job_id, True


def jobs_needing(stage: str, criteria_hash: str, limit: int) -> list[sqlite3.Row]:
    """Jobs with no evaluation for this stage under the current criteria."""
    with connect() as conn:
        return conn.execute(
            """SELECT j.* FROM jobs j
               LEFT JOIN user_state u ON u.job_id = j.id
               WHERE COALESCE(u.status, 'new') NOT IN ('dismissed', 'archived')
                 AND NOT EXISTS (
                       SELECT 1 FROM evaluations e
                       WHERE e.job_id = j.id AND e.stage = ?
                         AND e.criteria_hash = ?)
               ORDER BY j.first_seen DESC
               LIMIT ?""",
            (stage, criteria_hash, limit),
        ).fetchall()


def deepdive_candidates(criteria_hash: str, min_score: int, limit: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            """SELECT j.*, e.score AS triage_score FROM jobs j
               JOIN evaluations e ON e.job_id = j.id
                    AND e.stage = 'triage' AND e.criteria_hash = ?
               LEFT JOIN user_state u ON u.job_id = j.id
               WHERE e.score >= ?
                 AND COALESCE(u.status, 'new') NOT IN ('dismissed', 'archived')
                 AND NOT EXISTS (
                       SELECT 1 FROM evaluations d
                       WHERE d.job_id = j.id AND d.stage = 'deepdive'
                         AND d.criteria_hash = ?)
               ORDER BY e.score DESC
               LIMIT ?""",
            (criteria_hash, min_score, criteria_hash, limit),
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
) -> None:
    conn.execute(
        """INSERT INTO evaluations (job_id, run_id, stage, criteria_hash, score,
               verdict, eligible, summary, eligibility, salary, tech_stack,
               concerns, rationale, model, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            job_id, run_id, stage, criteria_hash, score, verdict,
            None if eligible is None else int(eligible), summary, eligibility,
            salary, json.dumps(list(tech_stack)), json.dumps(list(concerns)),
            rationale, model, utcnow(),
        ),
    )


BEST_EVAL_SQL = """
SELECT e.* FROM evaluations e
JOIN (SELECT job_id, MAX(id) AS mid FROM evaluations
      WHERE criteria_hash = ? GROUP BY job_id, stage) x ON x.mid = e.id
"""


def list_jobs(
    *, min_score: int = 0, status: str | None = None, source: str | None = None,
    query: str | None = None, remote: str | None = None,
    include_archived: bool = False, sort: str = "score",
    limit: int = 100, offset: int = 0, criteria_hash: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Jobs joined with their best current evaluation. Returns (page, total).

    `status=None` means the active statuses -- new, shortlisted, applied --
    unless include_archived, which shows everything. Pass an explicit status to
    see exactly that one, including 'archived' or 'dismissed'.
    """
    from .pipeline.criteria import current_criteria_hash

    ch = criteria_hash or current_criteria_hash()
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
            FROM evaluations WHERE criteria_hash = ? GROUP BY job_id
        ),
        stale AS (
            SELECT job_id, MAX(id) AS eid FROM evaluations WHERE criteria_hash != ? GROUP BY job_id
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
        LEFT JOIN user_state u ON u.job_id = j.id
        -- unevaluated jobs count as 0, so min_score=0 shows everything
        WHERE COALESCE(d.score, b.triage_score, o.score, 0) >= ?
    """
    params: list[Any] = [ch, ch, min_score]
    if status:
        sql += " AND COALESCE(u.status,'new') = ?"
        params.append(status)
    elif not include_archived:
        sql += " AND COALESCE(u.status,'new') NOT IN ('archived', 'dismissed')"
    if source:
        sql += " AND j.source_id = ?"
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


def get_job(job_id: int, criteria_hash: str | None = None) -> dict[str, Any] | None:
    from .pipeline.criteria import current_criteria_hash

    ch = criteria_hash or current_criteria_hash()
    with connect() as conn:
        row = conn.execute(
            """SELECT j.*, COALESCE(u.status,'new') AS status, u.notes, u.reason
               FROM jobs j LEFT JOIN user_state u ON u.job_id = j.id
               WHERE j.id = ?""",
            (job_id,),
        ).fetchone()
        if not row:
            return None
        job = dict(row)
        job["evaluations"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM evaluations WHERE job_id = ? ORDER BY id DESC", (job_id,)
            ).fetchall()
        ]
        job["current_criteria"] = ch
        return job


def set_user_state(job_id: int, status: str, notes: str | None = None,
                   reason: str | None = None) -> bool:
    """Record a decision. `reason` is kept only while the job is dismissed:
    restoring it clears the reason so it stops influencing scores."""
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
            return False
        if status != "dismissed":
            reason = None
        conn.execute(
            """INSERT INTO user_state (job_id, status, notes, reason, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 status = excluded.status,
                 notes = COALESCE(excluded.notes, user_state.notes),
                 reason = CASE WHEN excluded.status = 'dismissed'
                               THEN COALESCE(excluded.reason, user_state.reason)
                               ELSE NULL END,
                 updated_at = excluded.updated_at""",
            (job_id, status, notes, reason, utcnow()),
        )
        return True


def recent_rejections(limit: int = 20) -> list[dict[str, Any]]:
    """Dismissed jobs with a stated reason, newest first: the few-shot
    negative signal the prompts carry."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT j.company, j.title, j.location, j.remote_type, u.reason, u.updated_at
               FROM user_state u JOIN jobs j ON j.id = u.job_id
               WHERE u.status = 'dismissed' AND u.reason IS NOT NULL AND u.reason != ''
               ORDER BY u.updated_at DESC, u.job_id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_job(job_id: int) -> bool:
    """Permanently remove a job; evaluations and user_state cascade."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        return cur.rowcount > 0


def archive_jobs(*, older_than_days: int | None = None, ids: Iterable[int] | None = None,
                 only_status: str = "new") -> int:
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
                """INSERT INTO user_state (job_id, status, updated_at)
                   SELECT j.id, 'archived', ? FROM jobs j
                   LEFT JOIN user_state u ON u.job_id = j.id
                   WHERE COALESCE(u.status, 'new') = ?
                     AND datetime(j.last_seen) <= datetime('now', ?)
                   ON CONFLICT(job_id) DO UPDATE SET
                     status = 'archived', updated_at = excluded.updated_at
                   WHERE user_state.status != 'archived'""",
                (now, only_status, f"-{int(older_than_days)} days"),
            )
            changed += cur.rowcount
        for job_id in ids or []:
            cur = conn.execute(
                """INSERT INTO user_state (job_id, status, updated_at)
                   SELECT id, 'archived', ? FROM jobs WHERE id = ?
                   ON CONFLICT(job_id) DO UPDATE SET
                     status = 'archived', updated_at = excluded.updated_at
                   WHERE user_state.status != 'archived'""",
                (now, int(job_id)),
            )
            changed += cur.rowcount
    return changed


def facets() -> dict[str, Any]:
    """Distinct values the UI offers as filters, with counts."""
    with connect() as conn:
        by_status = {
            r["status"]: r["n"] for r in conn.execute(
                """SELECT COALESCE(u.status,'new') AS status, COUNT(*) AS n
                   FROM jobs j LEFT JOIN user_state u ON u.job_id = j.id
                   GROUP BY 1 ORDER BY 1"""
            )
        }
        by_source = {
            r["source_id"]: r["n"] for r in conn.execute(
                "SELECT source_id, COUNT(*) AS n FROM jobs GROUP BY 1 ORDER BY 2 DESC"
            )
        }
        by_remote = {
            r["remote_type"]: r["n"] for r in conn.execute(
                "SELECT COALESCE(remote_type,'unknown') AS remote_type, COUNT(*) AS n FROM jobs GROUP BY 1"
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
def upsert_source(cfg: dict[str, Any], origin: str = "config") -> bool:
    """Register a source. Returns True if it was newly added."""
    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM sources WHERE id = ?", (cfg["id"],)
        ).fetchone()
        if existing:
            # The YAML stays authoritative for WHAT a config source is, but not
            # for whether it is on: that is decided in the UI, and a re-sync on
            # every run must not switch a source back on. `enabled` in the YAML
            # therefore only applies when the row is first created.
            if origin == "config":
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
        return True


def active_sources() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sources WHERE enabled = 1 AND fail_count < 5 ORDER BY id"
        ).fetchall()
    return [json.loads(r["config"]) | {"_origin": r["origin"]} for r in rows]


def list_sources() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY id")]


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
    """Remove a source; its jobs stay. Config-origin sources cannot be deleted
    (the YAML would recreate them) -- disable those instead."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM sources WHERE id = ? AND origin != 'config'", (source_id,))
        return cur.rowcount > 0


def source_job_counts() -> dict[str, int]:
    with connect() as conn:
        return {r["source_id"]: r["n"] for r in conn.execute("SELECT source_id, COUNT(*) AS n FROM jobs GROUP BY 1")}


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
    with connect() as conn:
        conn.execute("VACUUM")
    return counts


def stale_score_count(criteria_hash: str) -> int:
    """Jobs whose only scores predate the current criteria: re-scored next scan."""
    with connect() as conn:
        return conn.execute(
            """SELECT COUNT(*) FROM jobs j
               WHERE EXISTS (SELECT 1 FROM evaluations e WHERE e.job_id = j.id)
                 AND NOT EXISTS (SELECT 1 FROM evaluations e WHERE e.job_id = j.id AND e.criteria_hash = ?)""",
            (criteria_hash,),
        ).fetchone()[0]


def stats() -> dict[str, Any]:
    with connect() as conn:
        q = lambda s, *p: conn.execute(s, p).fetchone()[0]  # noqa: E731
        return {
            "jobs": q("SELECT COUNT(*) FROM jobs"),
            "evaluated": q("SELECT COUNT(DISTINCT job_id) FROM evaluations"),
            "shortlisted": q("SELECT COUNT(*) FROM user_state WHERE status='shortlisted'"),
            "applied": q("SELECT COUNT(*) FROM user_state WHERE status='applied'"),
            "dismissed": q("SELECT COUNT(*) FROM user_state WHERE status='dismissed'"),
            "archived": q("SELECT COUNT(*) FROM user_state WHERE status='archived'"),
            "sources": q("SELECT COUNT(*) FROM sources WHERE enabled=1"),
            "discovered_sources": q("SELECT COUNT(*) FROM sources WHERE origin='discovered'"),
            "runs": q("SELECT COUNT(*) FROM runs"),
        }
