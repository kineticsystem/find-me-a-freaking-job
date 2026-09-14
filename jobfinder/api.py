"""HTTP API. Also the contract the React frontend will speak to later."""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, discovery, opencode, scheduler
from . import config as config_mod
from .config import ROOT, Preferences, preferences, settings, write_top_level_setting
from .config import reload as reload_config
from .pipeline import run as run_mod
from .pipeline import criteria as criteria_mod

log = logging.getLogger(__name__)

STATUSES = ("new", "shortlisted", "applied", "dismissed", "archived")
Status = Literal["new", "shortlisted", "applied", "dismissed", "archived"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    discovery.seed_from_config()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


app = FastAPI(title="find-me-a-freaking-job", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().api.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _decode_lists(job: dict[str, Any]) -> dict[str, Any]:
    """List fields are stored as JSON text and are NULL before any evaluation;
    the client always receives a list."""
    for field in ("tags", "tech_stack", "concerns"):
        raw = job.get(field)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = [raw] if raw else []
        job[field] = raw if isinstance(raw, list) else []
    return job


@app.get("/health")
def health() -> dict[str, Any]:
    from .pipeline import profile as prof
    from .pipeline import progress

    criteria = criteria_mod.current_criteria_hash()
    return {
        "ok": True,
        "setup": prof.readiness(),
        "running": run_mod.is_running(),
        "progress": progress.snapshot(),
        "stale_scores": db.stale_score_count(criteria),
        "next_run": scheduler.next_run(),
        "interval_minutes": settings().interval_minutes,
        "criteria": criteria,
        "opencode": opencode.health_check(),
        "stats": db.stats(),
    }


@app.get("/jobs")
def list_jobs(
    min_score: int = Query(0, ge=0, le=100),
    status: str | None = None,
    source: str | None = None,
    remote: str | None = None,
    q: str | None = None,
    hidden: bool = False,
    sort: Literal["score", "newest", "company"] = "score",
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Paged job list: the active jobs, or with hidden=true only the archived and dismissed ones."""
    if status and status not in STATUSES:
        raise HTTPException(400, f"status must be one of {STATUSES}")
    if remote and remote not in ("remote", "hybrid", "onsite", "unknown"):
        raise HTTPException(400, "remote must be one of remote|hybrid|onsite|unknown")
    rows, total = db.list_jobs(
        min_score=min_score, status=status, source=source, remote=remote, query=q,
        hidden=hidden, sort=sort, limit=limit, offset=offset,
    )
    return {
        "count": len(rows), "total": total, "offset": offset, "limit": limit,
        "jobs": [_decode_lists(r) for r in rows],
    }


@app.get("/jobs/facets")
def job_facets() -> dict[str, Any]:
    """Filter options with counts: statuses, sources, remote types."""
    return db.facets()


class ArchiveRequest(BaseModel):
    older_than_days: int | None = Field(None, ge=0, description="archive 'new' jobs not seen for this many days")
    ids: list[int] = Field(default_factory=list, description="archive these jobs regardless of age")


@app.post("/jobs/archive")
def archive_jobs(body: ArchiveRequest) -> dict[str, Any]:
    if body.older_than_days is None and not body.ids:
        raise HTTPException(400, "give older_than_days, ids, or both")
    changed = db.archive_jobs(older_than_days=body.older_than_days, ids=body.ids)
    return {"ok": True, "archived": changed}


@app.get("/jobs/{job_id}")
def get_job(job_id: int) -> dict[str, Any]:
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "no such job")
    return _decode_lists(job)


class StateUpdate(BaseModel):
    status: Status
    notes: str | None = None
    # Why it was dismissed. Free text; the UI offers chips such as "salary too
    # low" or "agency". Kept only while status is 'dismissed'.
    reason: str | None = Field(None, max_length=300)


@app.patch("/jobs/{job_id}/state")
def set_state(job_id: int, body: StateUpdate) -> dict[str, Any]:
    if not db.set_user_state(job_id, body.status, body.notes, body.reason):
        raise HTTPException(404, "no such job")
    return {"ok": True, "job_id": job_id, "status": body.status}


@app.get("/rejections")
def rejections(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    """What the model is currently told the candidate turned down, and why."""
    return {"rejections": db.recent_rejections(limit)}


@app.delete("/jobs/{job_id}")
def delete_job(job_id: int) -> dict[str, Any]:
    """Permanent. Prefer archiving; this is for junk that should never resurface."""
    if not db.delete_job(job_id):
        raise HTTPException(404, "no such job")
    return {"ok": True, "deleted": job_id}


@app.get("/runs")
def list_runs(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    return {"runs": db.list_runs(limit)}


@app.post("/runs")
def trigger_run() -> dict[str, Any]:
    from .pipeline import profile as prof

    ready = prof.readiness()
    if not ready["ready"]:
        missing = [k for k in ("cv", "notes", "preferences") if not ready[k]]
        raise HTTPException(409, "not scanning until set-up is complete; missing: " + ", ".join(missing))
    if run_mod.is_running():
        raise HTTPException(409, "a run is already in progress")
    if not scheduler.trigger_now():
        raise HTTPException(503, "scheduler is not running")
    return {"ok": True, "queued": True}


@app.post("/runs/stop")
def stop_run() -> dict[str, Any]:
    """Stop the running scan after its current unit; the in-flight model
    call is killed. Everything scored so far stays."""
    from .pipeline import progress

    if not progress.request_stop():
        raise HTTPException(409, "no scan is running")
    return {"ok": True, "stopping": True}


@app.get("/digest", response_class=PlainTextResponse)
def latest_digest() -> str:
    path = settings().paths.resolve("runs") / "latest-digest.md"
    if not path.exists():
        raise HTTPException(404, "no digest yet; wait for the first run to finish")
    return path.read_text()


@app.get("/sources")
def list_sources() -> dict[str, Any]:
    """Every source with what it has produced. `jobs_stored` is what is in the
    database right now from it; `jobs_found` is the running total fetched."""
    counts = db.source_job_counts()
    out = []
    for src in db.list_sources():
        cfg = json.loads(src["config"])
        out.append({**src, "config": cfg, "jobs_stored": counts.get(src["id"], 0),
                    "deletable": src["origin"] != "config"})
    return {"sources": out}


class SourceAdd(BaseModel):
    # Any company careers URL. A Greenhouse / Lever / Ashby board is recognised
    # from the URL or found behind the page; anything else is followed as a
    # rendered web page that the model reads.
    url: str = Field(..., min_length=8, max_length=500)


def _register_board(stype: str, slug: str, url: str, how: str) -> dict[str, Any]:
    from .sources import build

    cfg = discovery.source_config_for(stype, slug, url)
    source_id = cfg["id"]
    if db.get_source(source_id):
        raise HTTPException(409, f"{source_id} is already registered ({how})")
    try:
        found = build(cfg).fetch()
    except Exception as exc:
        raise HTTPException(400, f"{stype} board '{slug}' did not answer: {type(exc).__name__}: {exc}") from exc
    if not found:
        raise HTTPException(400, f"{stype} board '{slug}' answered but lists no open positions; not added")
    db.upsert_source(cfg, origin="user")
    db.mark_discovery(f"source:{source_id}", "source", url)
    return {"ok": True, "source_id": source_id, "type": stype, "slug": slug, "open_positions": len(found),
            "how": how, "note": "its postings will be fetched on the next scan"}


@app.post("/sources")
def add_source(body: SourceAdd) -> dict[str, Any]:
    """Register a company from its careers URL.

    1. A Greenhouse / Lever / Ashby URL is registered as that board.
    2. Otherwise the page is fetched and, if needed, rendered in a headless
       browser to find the ATS it loads its jobs from; found, that board is
       registered -- complete, structured, fast.
    3. Otherwise the page itself becomes a source: rendered on every scan
       and read by the model, which extracts the postings it lists.
    """
    from . import render as render_mod

    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    hit = discovery.detect_ats(url)
    if hit:
        return _register_board(*hit, url=url, how="from the URL")

    hit = discovery.sniff_ats(url)
    if hit:
        return _register_board(*hit, url=url, how="found behind the page")

    page = render_mod.render(url)
    if len(page.text) < 200:
        raise HTTPException(400, "That page renders to almost no text, so there is nothing to read. Is it the right URL?")
    host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", url).split("/")[0])
    source_id = "web-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
    if db.get_source(source_id):
        raise HTTPException(409, f"{source_id} is already registered")
    company = host.split(".")[0].replace("-", " ").title()
    cfg = {"id": source_id, "type": "webpage", "url": url, "enabled": True, "company": company, "added_from": url}
    db.upsert_source(cfg, origin="user")
    db.mark_discovery(f"source:{source_id}", "source", url)
    return {"ok": True, "source_id": source_id, "type": "webpage", "slug": host, "open_positions": None,
            "how": "no job board found behind the page; it will be read by the model on each scan",
            "note": "its postings will be extracted on the next scan"}


@app.post("/sources/{source_id}/enabled")
def toggle_source(source_id: str, enabled: bool = True) -> dict[str, Any]:
    if not db.set_source_enabled(source_id, enabled):
        raise HTTPException(404, "no such source")
    return {"ok": True, "source_id": source_id, "enabled": enabled}


@app.delete("/sources/{source_id}")
def remove_source(source_id: str) -> dict[str, Any]:
    src = db.get_source(source_id)
    if not src:
        raise HTTPException(404, "no such source")
    if src["origin"] == "config":
        raise HTTPException(400, "this source comes from config/sources.yaml; disable it instead of deleting it")
    db.delete_source(source_id)
    return {"ok": True, "deleted": source_id}


class SettingsUpdate(BaseModel):
    interval_minutes: int | None = Field(None, ge=5, le=7 * 24 * 60)
    run_on_start: bool | None = None


@app.get("/settings")
def get_settings() -> dict[str, Any]:
    """The settings the UI can change, plus scheduler state."""
    cfg = settings()
    return {
        "interval_minutes": cfg.interval_minutes,
        "run_on_start": cfg.run_on_start,
        "next_run": scheduler.next_run(),
        "running": run_mod.is_running(),
    }


@app.patch("/settings")
def update_settings(body: SettingsUpdate, request: Request) -> dict[str, Any]:
    """Persist to config/settings.yaml and apply live: no restart needed."""
    if body.interval_minutes is None and body.run_on_start is None:
        raise HTTPException(400, "nothing to change")
    log.warning("settings changed from %s: %s", request.client.host if request.client else "?",
                body.model_dump(exclude_none=True))
    # Validate the config files BEFORE touching them: a save that is going to
    # be refused because another file is broken must not change anything.
    errors = config_mod.check_config()
    if errors:
        raise HTTPException(400, errors[0].message)
    if body.interval_minutes is not None:
        write_top_level_setting("interval_minutes", body.interval_minutes)
        scheduler.set_interval(body.interval_minutes)
    if body.run_on_start is not None:
        write_top_level_setting("run_on_start", body.run_on_start)
    return get_settings()


# --------------------------------------------------------------------------
# profile: the CV and the notes, editable from the UI
# --------------------------------------------------------------------------
@app.get("/profile")
def get_profile() -> dict[str, Any]:
    from .pipeline import profile as prof

    return prof.profile_status()


@app.post("/profile/cv")
async def upload_cv(file: UploadFile = File(...)) -> dict[str, Any]:
    """Replace the CV. Saved as profile/cv.<ext>; any previous CV is removed.
    The profile digest is rebuilt and every job re-scored on the next run."""
    from .pipeline import profile as prof

    data = await file.read()
    try:
        saved = prof.replace_cv(file.filename or "", data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    status = prof.profile_status()
    if status["cv_chars"] == 0:
        raise HTTPException(400, f"{saved.name} was saved but no text could be extracted from it")
    return {"ok": True, "saved": saved.name, **status}


class NotesUpdate(BaseModel):
    text: str = Field(..., max_length=20000)


@app.put("/profile/notes")
def update_notes(body: NotesUpdate) -> dict[str, Any]:
    from .pipeline import profile as prof

    prof.write_notes(body.text)
    return {"ok": True, **prof.profile_status()}


# --------------------------------------------------------------------------
# resets: destructive, so the client must echo the exact confirmation word
# --------------------------------------------------------------------------
class ResetRequest(BaseModel):
    confirm: str = Field(..., description="must be exactly DELETE")


def _require_confirmation(body: ResetRequest) -> None:
    if body.confirm != "DELETE":
        raise HTTPException(400, 'type DELETE to confirm')
    if run_mod.is_running():
        raise HTTPException(409, "a scan is running; wait for it to finish, then try again")


@app.post("/reset/jobs")
def reset_jobs(body: ResetRequest) -> dict[str, Any]:
    """Delete every job, score, decision and run. Sources and your profile stay."""
    _require_confirmation(body)
    deleted = db.delete_all_jobs()
    log.warning("all jobs deleted from the UI: %s", deleted)
    return {"ok": True, "deleted": deleted}


@app.post("/reset/all")
def reset_all(body: ResetRequest) -> dict[str, Any]:
    """Delete everything in the database, sources included. Files (CV, notes,
    preferences) are untouched; the seed sources come back from config."""
    _require_confirmation(body)
    deleted = db.reset_everything()
    discovery.seed_from_config()
    log.warning("database reset from the UI: %s", deleted)
    return {"ok": True, "deleted": deleted, "sources_reseeded": len(db.list_sources())}


@app.get("/preferences")
def get_preferences() -> dict[str, Any]:
    return {
        "preferences": preferences().model_dump(),
        "config_path": str(config_mod.preferences_path()),
    }


@app.put("/preferences")
def put_preferences(body: Preferences) -> dict[str, Any]:
    """Save from the form. Validated by the same model the pipeline reads;
    location_rules order becomes the market priority. Re-scores everything on
    the next scan through the criteria hash."""
    config_mod.write_preferences(body.normalised())
    return get_preferences()


# --------------------------------------------------------------------------
# resets: destructive, so the client must echo the exact confirmation word
# --------------------------------------------------------------------------
class ResetRequest(BaseModel):
    confirm: str = Field(..., description="must be exactly DELETE")


def _require_confirmation(body: ResetRequest) -> None:
    if body.confirm != "DELETE":
        raise HTTPException(400, 'type DELETE to confirm')
    if run_mod.is_running():
        raise HTTPException(409, "a scan is running; wait for it to finish, then try again")


@app.post("/reset/jobs")
def reset_jobs(body: ResetRequest) -> dict[str, Any]:
    """Delete every job, score, decision and run. Sources and your profile stay."""
    _require_confirmation(body)
    deleted = db.delete_all_jobs()
    log.warning("all jobs deleted from the UI: %s", deleted)
    return {"ok": True, "deleted": deleted}


@app.post("/reset/all")
def reset_all(body: ResetRequest) -> dict[str, Any]:
    """Delete everything in the database, sources included. Files (CV, notes,
    preferences) are untouched; the seed sources come back from config."""
    _require_confirmation(body)
    deleted = db.reset_everything()
    discovery.seed_from_config()
    log.warning("database reset from the UI: %s", deleted)
    return {"ok": True, "deleted": deleted, "sources_reseeded": len(db.list_sources())}


@app.get("/preferences")
def get_preferences() -> dict[str, Any]:
    return {
        "preferences": preferences().model_dump(),
        "config_path": str(config_mod.preferences_path()),
    }


@app.put("/preferences")
def put_preferences(body: Preferences) -> dict[str, Any]:
    """Save from the form. Validated by the same model the pipeline reads;
    location_rules order becomes the market priority. Re-scores everything on
    the next scan through the criteria hash."""
    config_mod.write_preferences(body.normalised())
    return get_preferences()


@app.post("/reload")
def reload() -> dict[str, Any]:
    """Pick up edited YAML without restarting; re-seeds sources. A broken
    file is reported and the previous values stay in force."""
    try:
        reload_config()
    except config_mod.ConfigError as exc:
        raise HTTPException(400, exc.message) from exc
    added = discovery.seed_from_config()
    return {"ok": True, "new_sources": added, "criteria": criteria_mod.current_criteria_hash()}


# --------------------------------------------------------------------------
# Web UI: serve web/dist when it has been built, so one process on one port
# is all that needs exposing to the internet. API routes above take priority.
# --------------------------------------------------------------------------
WEB_DIST = ROOT / "web" / "dist"

if (WEB_DIST / "index.html").exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        candidate = WEB_DIST / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(WEB_DIST / "index.html")  # client-side routing
