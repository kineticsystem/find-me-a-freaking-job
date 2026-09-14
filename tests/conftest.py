"""Every test gets a fresh SQLite file; nothing touches data/jobs.db."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobfinder import db  # noqa: E402
from jobfinder.models import NormalizedJob, fingerprint  # noqa: E402


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "db_path", lambda: path)
    db.init_db()
    return path


@pytest.fixture()
def criteria(monkeypatch):
    """Pin the criteria hash so tests don't depend on the real CV on disk."""
    from jobfinder.pipeline import criteria as crit

    monkeypatch.setattr(crit, "current_criteria_hash", lambda: "test-criteria")
    return "test-criteria"


def make_job(company: str, title: str, location: str = "Remote", **kw) -> NormalizedJob:
    return NormalizedJob(
        fingerprint=fingerprint(company, title, location),
        source_id=kw.pop("source_id", "test"),
        company=company,
        title=title,
        location=location,
        remote_type=kw.pop("remote_type", "remote"),
        url=f"https://example.com/{company}/{title}".replace(" ", "-"),
        apply_url="",
        description=kw.pop("description", f"{title} at {company}"),
        **kw,
    )


@pytest.fixture()
def seeded(tmp_db, criteria):
    """Three jobs: one strong (deep-dived), one triaged only, one unevaluated."""
    with db.connect() as conn:
        a, _ = db.upsert_job(conn, make_job("Acme", "Senior C++ Engineer", "Remote US"))
        b, _ = db.upsert_job(conn, make_job("Globex", "Python Backend Developer", "Amsterdam, Netherlands",
                                            remote_type="hybrid", source_id="gh-globex"))
        c, _ = db.upsert_job(conn, make_job("Initech", "Registered Nurse", "Ohio", remote_type="onsite"))
        db.record_evaluation(conn, job_id=a, run_id=None, stage="triage", criteria_hash=criteria,
                             score=85, verdict="strong", model="m")
        db.record_evaluation(conn, job_id=a, run_id=None, stage="deepdive", criteria_hash=criteria,
                             score=92, verdict="strong", model="m", eligible=True,
                             summary="Great C++ role", tech_stack=["C++", "Qt"])
        db.record_evaluation(conn, job_id=b, run_id=None, stage="triage", criteria_hash=criteria,
                             score=55, verdict="maybe", model="m")
    return {"a": a, "b": b, "c": c}


@pytest.fixture()
def client(seeded):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import jobfinder.api as api

    # Reuse the routes but not the lifespan: no scheduler in tests.
    app = FastAPI()
    app.router.routes = list(api.app.router.routes)
    with TestClient(app) as c:
        yield c
