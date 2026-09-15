"""Every test gets a fresh SQLite file; nothing touches data/jobs.db."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobfinder import db  # noqa: E402
from jobfinder.models import NormalizedJob, fingerprint  # noqa: E402


@pytest.fixture(autouse=True)
def _never_touch_real_files(tmp_path, monkeypatch):
    """Every test runs against a throwaway config directory. A test once
    PATCHed /settings against the real config/settings.yaml, and the
    author's scan interval kept changing after every test run."""
    import jobfinder.config as config

    cfg_dir = tmp_path / "config"; cfg_dir.mkdir()
    for name in ("settings.example.yaml", "preferences.example.yaml"):
        (cfg_dir / name).write_text((config.CONFIG_DIR / name).read_text())
    (cfg_dir / "settings.yaml").write_text("interval_minutes: 720\n")
    (cfg_dir / "preferences.yaml").write_text("based_in: Testland\ntitles: [Engineer]\n")
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "settings_path", lambda: cfg_dir / "settings.yaml")
    monkeypatch.setattr(config, "preferences_path", lambda: cfg_dir / "preferences.yaml")
    config.settings.cache_clear(); config.preferences.cache_clear()
    yield
    config.settings.cache_clear(); config.preferences.cache_clear()


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
def anon_client(seeded):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import jobfinder.api as api

    # Reuse the routes but not the lifespan: no scheduler in tests.
    app = FastAPI()
    app.router.routes = list(api.app.router.routes)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def client(anon_client):
    """Logged in as the admin (user 1), who owns the seeded data. Tests that
    need the anonymous client or a second user take ``anon_client``."""
    from jobfinder import auth
    from jobfinder.config import DEFAULT_USER_ID

    db.claim_user(DEFAULT_USER_ID, "admin@example.com", auth.hash_password("admin-pass-1"), is_admin=True)
    r = anon_client.post("/auth/login", json={"email": "admin@example.com", "password": "admin-pass-1"})
    anon_client.headers["Authorization"] = f"Bearer {r.json()['token']}"
    return anon_client
