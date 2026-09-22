"""HTTP API. Also the contract the React frontend will speak to later."""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, db, discovery, opencode, scheduler
from .auth import AdminUser, CurrentUser
from . import config as config_mod
from .config import ROOT, Preferences, preferences, settings, write_top_level_setting
from .config import reload as reload_config
from .pipeline import run as run_mod

log = logging.getLogger(__name__)

# declined: applied, then turned down -- kept apart from dismissed (your
# choice) and archived (no longer relevant) so the record of applications
# that went nowhere is one filter away.
STATUSES = ("new", "shortlisted", "applied", "declined", "dismissed", "archived")
Status = Literal["new", "shortlisted", "applied", "declined", "dismissed", "archived"]


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


# /health and /auth/login are the only routes without a token. Per-user
# routes read the user from the token; routes that touch the shared
# installation (settings, profile files, sources, scans, resets) are admin
# only until those become per user too.
user_api = APIRouter(dependencies=[Depends(auth.current_user)])
admin_api = APIRouter(dependencies=[Depends(auth.admin_user)])


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
def health(request: Request) -> dict[str, Any]:
    """Open to everyone for the installation's state; the per-user parts
    (set-up checklist, stale scores, counts) are filled in when a valid
    token comes along, and null otherwise."""
    from .pipeline import profile as prof
    from .pipeline import progress

    user = auth.optional_user(request)
    out: dict[str, Any] = {
        "ok": True,
        "needs_setup": not db.any_user_can_login(),  # no account yet: the UI shows the first-user screen
        "running": run_mod.is_running(),
        "progress": progress.snapshot(),
        "next_run": scheduler.next_run(),
        "last_run": db.last_finished_run(),
        "interval_minutes": settings().interval_minutes,
        "opencode": opencode.health_check(),
        "setup": None, "stale_scores": None, "criteria": None, "stats": None,
    }
    if user:
        cand = prof.load(user["id"])
        out.update({
            "setup": cand.readiness,
            "stale_scores": db.stale_score_count(cand.criteria, user_id=user["id"]),
            "criteria": cand.criteria,
            "stats": db.stats(user_id=user["id"]),
        })
    return out


@user_api.get("/jobs")
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
    user: CurrentUser = None,
) -> dict[str, Any]:
    """Paged job list: the active jobs, or with hidden=true only the archived and dismissed ones."""
    if status and status not in STATUSES:
        raise HTTPException(400, f"status must be one of {STATUSES}")
    if remote and remote not in ("remote", "hybrid", "onsite", "unknown"):
        raise HTTPException(400, "remote must be one of remote|hybrid|onsite|unknown")
    rows, total = db.list_jobs(
        min_score=min_score, status=status, source=source, remote=remote, query=q,
        hidden=hidden, sort=sort, limit=limit, offset=offset, user_id=user["id"],
    )
    return {
        "count": len(rows), "total": total, "offset": offset, "limit": limit,
        "jobs": [_decode_lists(r) for r in rows],
    }


@user_api.get("/jobs/facets")
def job_facets(user: CurrentUser) -> dict[str, Any]:
    """Filter options with counts: statuses, sources, remote types."""
    return db.facets(user_id=user["id"])


class ArchiveRequest(BaseModel):
    older_than_days: int | None = Field(None, ge=0, description="archive 'new' jobs not seen for this many days")
    ids: list[int] = Field(default_factory=list, description="archive these jobs regardless of age")


@user_api.post("/jobs/archive")
def archive_jobs(body: ArchiveRequest, user: CurrentUser) -> dict[str, Any]:
    if body.older_than_days is None and not body.ids:
        raise HTTPException(400, "give older_than_days, ids, or both")
    changed = db.archive_jobs(older_than_days=body.older_than_days, ids=body.ids, user_id=user["id"])
    return {"ok": True, "archived": changed}


@user_api.get("/jobs/{job_id}")
def get_job(job_id: int, user: CurrentUser) -> dict[str, Any]:
    job = db.get_job(job_id, user_id=user["id"])
    if not job:
        raise HTTPException(404, "no such job")
    return _decode_lists(job)


class StateUpdate(BaseModel):
    status: Status
    notes: str | None = None
    # Why it was dismissed. Free text; the UI offers chips such as "salary too
    # low" or "agency". Kept only while status is 'dismissed'.
    reason: str | None = Field(None, max_length=300)


@user_api.patch("/jobs/{job_id}/state")
def set_state(job_id: int, body: StateUpdate, user: CurrentUser) -> dict[str, Any]:
    if not db.set_user_state(job_id, body.status, body.notes, body.reason, user_id=user["id"]):
        raise HTTPException(404, "no such job")
    return {"ok": True, "job_id": job_id, "status": body.status}


@user_api.get("/rejections")
def rejections(user: CurrentUser, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    """What the model is currently told the candidate turned down, and why."""
    return {"rejections": db.recent_rejections(limit, user_id=user["id"])}


@user_api.delete("/jobs/{job_id}")
def delete_job(job_id: int) -> dict[str, Any]:
    """Permanent. Prefer archiving; this is for junk that should never resurface."""
    if not db.delete_job(job_id):
        raise HTTPException(404, "no such job")
    return {"ok": True, "deleted": job_id}


@admin_api.get("/runs")
def list_runs(limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    return {"runs": db.list_runs(limit)}


@admin_api.post("/runs")
def trigger_run(admin: AdminUser) -> dict[str, Any]:
    """A scan fetches everything and scores every unscored posting for
    every user with a complete profile; it needs at least one. Hours on a
    first run or after a CV change, minutes once caught up; Stop is
    honoured in every stage."""
    from .pipeline import profile as prof

    if not prof.candidates():
        ready = prof.readiness(admin["id"])
        missing = [k for k in ("cv", "notes", "preferences") if not ready[k]]
        raise HTTPException(409, "not scanning until at least one profile is complete; yours is missing: " + ", ".join(missing))
    if run_mod.is_running():
        raise HTTPException(409, "a run is already in progress")
    if not scheduler.trigger_now():
        raise HTTPException(503, "scheduler is not running")
    return {"ok": True, "queued": True}


@admin_api.post("/runs/stop")
def stop_run() -> dict[str, Any]:
    """Stop the running scan after its current unit; the in-flight model
    call is killed. Everything scored so far stays."""
    from .pipeline import progress

    if not progress.request_stop():
        raise HTTPException(409, "no scan is running")
    return {"ok": True, "stopping": True}


@admin_api.get("/digest", response_class=PlainTextResponse)
def latest_digest() -> str:
    path = settings().paths.resolve("runs") / "latest-digest.md"
    if not path.exists():
        raise HTTPException(404, "no digest yet; wait for the first run to finish")
    return path.read_text()


@user_api.get("/sources")
def list_sources(user: CurrentUser) -> dict[str, Any]:
    """This user's list: the seed sources, what they added, what was
    discovered through their boards, their own CV searches. `following` is
    their switch. `jobs_stored` is what is in the database right now from
    it; `jobs_found` the running total fetched. Seed rows cannot be removed;
    the rest can, from this list only."""
    counts = db.source_job_counts()
    out = []
    for src in db.list_sources(user["id"]):
        cfg = json.loads(src["config"])
        if src["origin"] == "keyword":      # labelled from the query itself, never from what a past version stored
            cfg = {**cfg, "keyword": cfg.get("tag"), "region": cfg.get("geo", ""),
                   "company": f'"{cfg.get("tag")}" · {cfg.get("geo", "")}'}
        out.append({**src, "config": cfg, "jobs_stored": counts.get(src["id"], 0),
                    "deletable": src["origin"] != "config"})
    return {"sources": out}


class SourceAdd(BaseModel):
    # Any company careers URL. A Greenhouse / Lever / Ashby board is recognised
    # from the URL or found behind the page; anything else is followed as a
    # rendered web page that the model reads.
    url: str = Field(..., min_length=8, max_length=500)


def _follow_existing(user_id: int, source_id: str, how: str) -> dict[str, Any]:
    """Somebody else already added it: start following the existing row."""
    if db.follows(user_id, source_id):
        raise HTTPException(409, f"{source_id} is already in your list ({how})")
    db.follow_source(user_id, source_id, True)
    return {"ok": True, "source_id": source_id, "how": how, "open_positions": None,
            "note": "already registered by someone else; it is now in your list too"}


def _register_board(stype: str, slug: str, url: str, how: str, user_id: int) -> dict[str, Any]:
    from .sources import build

    cfg = discovery.source_config_for(stype, slug, url)
    source_id = cfg["id"]
    if db.get_source(source_id):
        return _follow_existing(user_id, source_id, how)
    try:
        found = build(cfg).fetch()
    except Exception as exc:
        raise HTTPException(400, f"{stype} board '{slug}' did not answer: {type(exc).__name__}: {exc}") from exc
    if not found:
        raise HTTPException(400, f"{stype} board '{slug}' answered but lists no open positions; not added")
    db.upsert_source(cfg, origin="user", followers=[user_id])
    db.mark_discovery(f"source:{source_id}", "source", url)
    return {"ok": True, "source_id": source_id, "type": stype, "slug": slug, "open_positions": len(found),
            "how": how, "note": "its postings will be fetched on the next scan"}


@user_api.post("/sources")
def add_source(body: SourceAdd, user: CurrentUser) -> dict[str, Any]:
    """Add a company from its careers URL to your list. The registry row is
    shared -- a URL somebody else already added is followed, not duplicated.

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
        return _register_board(*hit, url=url, how="from the URL", user_id=user["id"])

    try:
        hit = discovery.sniff_ats(url)
    except Exception as exc:  # noqa: BLE001 - never a 500 for a bad URL
        raise HTTPException(400, f"Could not reach that page: {type(exc).__name__}: {exc}") from exc
    if hit:
        return _register_board(*hit, url=url, how="found behind the page", user_id=user["id"])

    page = render_mod.render(url)
    if len(page.text) < 200:
        raise HTTPException(400, "That page could not be fetched, or renders to almost no text, so there is nothing to read. Is it the right URL?")
    host = re.sub(r"^www\.", "", re.sub(r"^https?://", "", url).split("/")[0])
    source_id = "web-" + re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")
    if db.get_source(source_id):
        return _follow_existing(user["id"], source_id, "the page is already registered")
    company = host.split(".")[0].replace("-", " ").title()
    cfg = {"id": source_id, "type": "webpage", "url": url, "enabled": True, "company": company, "added_from": url}
    db.upsert_source(cfg, origin="user", followers=[user["id"]])
    db.mark_discovery(f"source:{source_id}", "source", url)
    return {"ok": True, "source_id": source_id, "type": "webpage", "slug": host, "open_positions": None,
            "how": "no job board found behind the page; it will be read by the model on each scan",
            "note": "its postings will be extracted on the next scan"}


@user_api.post("/sources/{source_id}/enabled")
def toggle_source(source_id: str, user: CurrentUser, enabled: bool = True) -> dict[str, Any]:
    """Your own switch: on means fetched for you and its postings shown to
    you; off hides them (except what you already decided on) and, if nobody
    else follows it, stops it being fetched at all."""
    if not db.in_list(user["id"], source_id) or not db.follow_source(user["id"], source_id, enabled):
        raise HTTPException(404, "no such source in your list")
    return {"ok": True, "source_id": source_id, "enabled": enabled}


@user_api.delete("/sources/{source_id}")
def remove_source(source_id: str, user: CurrentUser) -> dict[str, Any]:
    """Remove a source from your list. Others who have it keep it; the
    shared row is dropped only once nobody has it. Seed-list rows cannot be
    removed, only switched off. Its jobs stay either way."""
    src = db.get_source(source_id)
    if not src or not db.in_list(user["id"], source_id):
        raise HTTPException(404, "no such source in your list")
    if src["origin"] == "config":
        raise HTTPException(400, "this source comes from config/sources.yaml; switch it off instead of removing it")
    outcome = db.unfollow_source(user["id"], source_id)
    return {"ok": True, "deleted": source_id, "dropped_for_everyone": outcome == "dropped"}


class SettingsUpdate(BaseModel):
    interval_minutes: int | None = Field(None, ge=5, le=7 * 24 * 60)
    run_on_start: bool | None = None


@admin_api.get("/settings")
def get_settings() -> dict[str, Any]:
    """The settings the UI can change, plus scheduler state."""
    cfg = settings()
    return {
        "interval_minutes": cfg.interval_minutes,
        "run_on_start": cfg.run_on_start,
        "next_run": scheduler.next_run(),
        "running": run_mod.is_running(),
    }


@admin_api.patch("/settings")
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
# profile: the current user's CV and notes, in the database
# --------------------------------------------------------------------------
@user_api.get("/profile")
def get_profile(user: CurrentUser) -> dict[str, Any]:
    from .pipeline import profile as prof

    return prof.profile_status(user["id"])


@user_api.get("/profile/cv")
def download_cv(user: CurrentUser) -> Response:
    """The CV as uploaded, for the person it belongs to."""
    found = db.get_cv_blob(user["id"])
    if not found:
        raise HTTPException(404, "no CV uploaded")
    name, data = found
    media = "application/pdf" if name.endswith(".pdf") else "text/plain; charset=utf-8"
    return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@user_api.post("/profile/cv")
async def upload_cv(user: CurrentUser, file: UploadFile = File(...)) -> dict[str, Any]:
    """Replace the CV; the previous one is gone. The profile digest is
    rebuilt and every job re-scored for this user on the next run."""
    from .pipeline import profile as prof

    data = await file.read()
    try:
        saved = prof.replace_cv(user["id"], file.filename or "", data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    status = prof.profile_status(user["id"])
    if status["cv_chars"] == 0:
        raise HTTPException(400, f"{saved} was saved but no text could be extracted from it")
    return {"ok": True, "saved": saved, **status}


class NotesUpdate(BaseModel):
    text: str = Field(..., max_length=20000)


@user_api.put("/profile/notes")
def update_notes(body: NotesUpdate, user: CurrentUser) -> dict[str, Any]:
    from .pipeline import profile as prof

    prof.write_notes(user["id"], body.text)
    return {"ok": True, **prof.profile_status(user["id"])}


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


@admin_api.post("/reset/jobs")
def reset_jobs(body: ResetRequest) -> dict[str, Any]:
    """Delete every job, score, decision and run. Sources and profiles stay."""
    _require_confirmation(body)
    deleted = db.delete_all_jobs()
    log.warning("all jobs deleted from the UI: %s", deleted)
    return {"ok": True, "deleted": deleted}


@admin_api.post("/reset/all")
def reset_all(body: ResetRequest) -> dict[str, Any]:
    """Delete everything in the database, sources included. Files (CV, notes,
    preferences) are untouched; the seed sources come back from config."""
    _require_confirmation(body)
    deleted = db.reset_everything()
    discovery.seed_from_config()
    log.warning("database reset from the UI: %s", deleted)
    return {"ok": True, "deleted": deleted, "sources_reseeded": len(db.list_sources())}


@user_api.get("/preferences")
def get_preferences(user: CurrentUser) -> dict[str, Any]:
    """The current user's preferences document (one per user, in the database)."""
    return {"preferences": preferences(user["id"]).model_dump(), "user_id": user["id"]}


@user_api.put("/preferences")
def put_preferences(body: Preferences, user: CurrentUser) -> dict[str, Any]:
    """Save from the form. Validated by the same model the pipeline reads;
    location_rules order becomes the market priority. Re-scores everything on
    the next scan through the criteria hash."""
    config_mod.save_preferences(body.normalised(), user_id=user["id"])
    return get_preferences(user)


@admin_api.post("/reload")
def reload() -> dict[str, Any]:
    """Pick up edited YAML without restarting; re-seeds sources. A broken
    file is reported and the previous values stay in force."""
    try:
        reload_config()
    except config_mod.ConfigError as exc:
        raise HTTPException(400, exc.message) from exc
    added = discovery.seed_from_config()
    return {"ok": True, "new_sources": added}


# --------------------------------------------------------------------------
# Login and accounts
# --------------------------------------------------------------------------
class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=200)
    password: str = Field(..., min_length=1, max_length=1000)
    device: str = Field("", max_length=100, description="label for this token, e.g. the browser")


@app.post("/auth/login")
def auth_login(body: LoginRequest) -> dict[str, Any]:
    """Email + password → a bearer token, shown once. Send it as
    ``Authorization: Bearer <token>`` on every other request."""
    result = auth.login(body.email, body.password, name=body.device)
    if not result:
        raise HTTPException(401, "wrong email or password")
    return result


@user_api.post("/auth/logout")
def auth_logout(user: CurrentUser, everywhere: bool = False) -> dict[str, Any]:
    """Revokes this token; with everywhere=true every token of the user."""
    if everywhere:
        db.delete_user_tokens(user["id"])
    else:
        db.delete_token(user["token_id"])
    return {"ok": True}


@user_api.get("/auth/me")
def auth_me(user: CurrentUser) -> dict[str, Any]:
    return {k: v for k, v in user.items() if k != "token_id"}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, max_length=1000)


@user_api.put("/auth/password")
def auth_change_password(body: PasswordChange, user: CurrentUser) -> dict[str, Any]:
    """Changing the password logs out every other device; this one stays."""
    if not auth.check_password(user["id"], body.current_password):
        raise HTTPException(401, "wrong current password")
    db.set_password_hash(user["id"], auth.hash_password(body.new_password))
    db.delete_user_tokens(user["id"], keep=user["token_id"])
    return {"ok": True}


class UserCreate(BaseModel):
    email: str = Field(..., min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(..., min_length=8, max_length=1000)
    is_admin: bool = False


@app.post("/auth/setup")
def auth_setup(body: UserCreate) -> dict[str, Any]:
    """First account only, open while nobody can log in: claims the pre-login
    default user, so everything already in the database becomes this
    person's. Refused (409) once any account exists."""
    if db.any_user_can_login():
        raise HTTPException(409, "an account already exists; ask the admin")
    if db.get_user_by_email(body.email) and db.get_user_by_email(body.email)["id"] != config_mod.DEFAULT_USER_ID:
        raise HTTPException(409, "that email is taken")
    db.claim_user(config_mod.DEFAULT_USER_ID, body.email, auth.hash_password(body.password), is_admin=True)
    return auth.login(body.email, body.password, name="first login") or {}


@admin_api.get("/users")
def list_users() -> dict[str, Any]:
    return {"users": db.list_users()}


@admin_api.post("/users")
def create_user(body: UserCreate) -> dict[str, Any]:
    if db.get_user_by_email(body.email):
        raise HTTPException(409, "that email is taken")
    uid = db.create_user(body.email, auth.hash_password(body.password), is_admin=body.is_admin)
    return {"ok": True, "user": db.get_user(uid)}


class PasswordReset(BaseModel):
    password: str = Field(..., min_length=8, max_length=1000)


@admin_api.put("/users/{user_id}/password")
def reset_user_password(user_id: int, body: PasswordReset) -> dict[str, Any]:
    """Admin sets a new password for someone who forgot theirs; their tokens are revoked."""
    if not db.set_password_hash(user_id, auth.hash_password(body.password)):
        raise HTTPException(404, "no such user")
    db.delete_user_tokens(user_id)
    return {"ok": True}


@admin_api.delete("/users/{user_id}")
def delete_user(user_id: int, admin: AdminUser) -> dict[str, Any]:
    """Removes the account with its tokens, decisions, scores and preferences."""
    if user_id == admin["id"]:
        raise HTTPException(400, "you cannot delete yourself")
    if not db.delete_user(user_id):
        raise HTTPException(404, "no such user")
    return {"ok": True, "deleted": user_id}


app.include_router(user_api)
app.include_router(admin_api)


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
