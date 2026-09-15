"""The whole run, start to finish. One of these fires on every interval tick."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import db, discovery, opencode
from ..config import settings
from ..models import RawJob
from ..sources import build
from . import deepdive, digest as digest_mod, extract, fetch, profile, progress, triage

log = logging.getLogger(__name__)

# Only one pipeline at a time: the scheduler tick and a manual API trigger must
# never interleave (sqlite writes and a single busy LLM).
_run_lock = threading.Lock()


def is_running() -> bool:
    return _run_lock.locked()


def _new_workdir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = settings().paths.resolve("runs") / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_once(*, skip_llm: bool = False) -> dict[str, Any]:
    """Execute one full pipeline. Returns the stats dict recorded on the run."""
    if not _run_lock.acquire(blocking=False):
        log.warning("a run is already in progress; skipping this tick")
        return {"skipped": "already running"}

    # No profile, no scan at all -- not even fetching. Postings collected
    # before a CV, notes and preferences exist could not be judged, and the
    # system stays idle until it knows who it works for. With several users,
    # one complete profile is enough to scan; the others are skipped.
    cands = profile.candidates()
    if not cands:
        _run_lock.release()
        first = profile.readiness(db.DEFAULT_USER_ID)
        missing = [k for k in ("cv", "notes", "preferences") if not first[k]]
        log.warning("not scanning: no user has a complete profile (see the web app)")
        return {"skipped": "not set up", "not_ready": missing}

    started = time.time()
    workdir = _new_workdir()
    run_id = db.start_run()
    stats: dict[str, Any] = {"run_id": run_id, "workdir": str(workdir), "skip_llm": skip_llm}
    log.info("run %d starting in %s", run_id, workdir)
    progress.begin(run_id)

    try:
        discovery.seed_from_config()

        # -- is there a model to talk to? --------------------------------
        # Fetching is still worth doing without one; scoring waits for the
        # next run. Without this check a downed server costs ~a minute per
        # session, times every batch.
        status = "ok"
        if not skip_llm:
            reachable, why = opencode.llm_reachable(wait_seconds=settings().llm.startup_wait_seconds)
            if not reachable:
                log.error("model server unreachable, skipping LLM stages: %s", why)
                stats["llm_unreachable"] = why
                skip_llm = True
                status = "partial"
        stats["skip_llm"] = skip_llm

        # -- profiles ----------------------------------------------------
        # One digest per user; the model builds it only when the CV or the
        # notes changed. Without a model, users with no stored digest are
        # fetched for but not scored.
        if not skip_llm:
            progress.stage("profile", "reading CVs and notes")
            for cand in cands:
                profile.load_digest(cand, workdir)
        stats["users"] = {c.user_id: {"name": c.name, "criteria": c.criteria, "profile": c.digest.headline if c.digest else None} for c in cands}
        stats["criteria"] = cands[0].criteria   # kept for older readers of the stats
        with_digest = [c for c in cands if c.digest]

        # -- fetch (persistent sources + ephemeral keyword queries) ------
        # Keyless sources once for everyone; keyword queries per user.
        progress.stage("fetch", "fetching postings from every source")
        raw, fetch_stats = fetch.fetch_all(workdir, with_digest[0].digest if with_digest else None)
        stats.update(fetch_stats)
        progress.note(fetched=len(raw), sources=fetch_stats.get("sources_run", 0))

        if with_digest and settings().discovery.enabled:
            for cand in with_digest:
                raw += _fetch_keyword_sources(cand, workdir, stats)

        # -- discovery channel A: harvest boards out of what we fetched --
        stats["harvested_sources"] = discovery.harvest(raw)

        # -- structure the prose sources ---------------------------------
        if not skip_llm:
            progress.stage("extract", "structuring free-text adverts")
            structured = extract.extract(raw, workdir)
            raw = [j for j in raw if not j.needs_extraction] + structured
            stats["extracted"] = len(structured)
        else:
            raw = [j for j in raw if not j.needs_extraction]

        # -- cheap local relevance gate ----------------------------------
        # A posting is stored if it passes the gate for at least one user;
        # storage is shared, each user's slice is a query over it.
        kept, pf_stats = fetch.prefilter_jobs(raw, [c.digest for c in with_digest])
        stats.update(pf_stats)

        # -- persist ------------------------------------------------------
        progress.stage("store", "storing new postings")
        store_stats = fetch.store(kept, settings().limits.max_jobs_per_run)
        store_stats.pop("new_ids", None)
        stats.update(store_stats)

        # -- reasoning, per user ------------------------------------------
        if not skip_llm:
            for cand in with_digest:
                if progress.stop_requested():
                    break
                who = stats["users"][cand.user_id]
                who.update(triage.run_triage(cand, workdir, run_id))
                if not progress.stop_requested():
                    who.update(deepdive.run_deepdive(cand, workdir, run_id))
                if not progress.stop_requested():
                    progress.stage("discover", f"looking for new company boards for {cand.name}")
                    who["websearch"] = discovery.run_websearch(cand, workdir)
            for key in ("triaged", "batches", "failed_batches", "strong", "deepdived"):
                stats[key] = sum(int(u.get(key) or 0) for u in stats["users"].values())
        if progress.stop_requested():
            status = "stopped"
            stats["stopped"] = True
            log.info("run %d stopped on request; what was scored so far is kept", run_id)

        stats["duration_seconds"] = round(time.time() - started, 1)
        digest_path = digest_mod.write_digest(workdir, run_id, cands, stats)
        stats["digest"] = str(digest_path)

        db.finish_run(run_id, status, stats)
        log.info("run %d finished (%s) in %.1fs: %s", run_id, status, stats["duration_seconds"], stats)
        return stats

    except opencode.SessionCancelled:
        stats["duration_seconds"] = round(time.time() - started, 1)
        stats["stopped"] = True
        db.finish_run(run_id, "stopped", stats)
        log.info("run %d stopped on request during a model call; what was scored so far is kept", run_id)
        return stats
    except Exception as exc:
        stats["duration_seconds"] = round(time.time() - started, 1)
        error = f"{type(exc).__name__}: {exc}"
        log.error("run %d failed: %s\n%s", run_id, error, traceback.format_exc())
        (workdir / "error.log").write_text(traceback.format_exc())
        db.finish_run(run_id, "error", stats, error)
        stats["error"] = error
        return stats
    finally:
        progress.finish()
        _run_lock.release()


def _fetch_keyword_sources(cand: profile.Candidate, workdir: Path, stats: dict[str, Any]) -> list[RawJob]:
    """Channel B: per-run keyword queries, per user. Failures are non-fatal."""
    out: list[RawJob] = []
    found: dict[str, Any] = {}
    current = discovery.keyword_sources(cand)
    db.prune_keyword_sources(cand.user_id, [c["id"] for c in current])
    for cfg in current:
        db.upsert_source(cfg, origin="keyword", followers=[cand.user_id])
        if not db.follows(cand.user_id, cfg["id"]):
            continue                                   # switched off in their list
        try:
            jobs = build(dict(cfg, _workdir=str(workdir))).fetch()
            out.extend(jobs)
            found[cfg["id"]] = len(jobs)
            db.record_source_result(cfg["id"], len(jobs))
        except Exception as exc:
            found[cfg["id"]] = f"{type(exc).__name__}: {exc}"
            db.record_source_result(cfg["id"], 0, str(exc))
            log.warning("keyword source %s failed: %s", cfg["id"], exc)
    stats["users"][cand.user_id]["keyword_queries"] = found
    stats["keyword_raw"] = stats.get("keyword_raw", 0) + len(out)
    return out
