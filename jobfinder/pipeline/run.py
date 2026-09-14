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
from . import deepdive, digest as digest_mod, extract, fetch, profile, triage
from .criteria import criteria_hash

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
    # before the CV, notes and preferences exist could not be judged, and the
    # user asked for the system to stay idle until it knows who it works for.
    ready = profile.readiness()
    if not ready["ready"]:
        _run_lock.release()
        missing = [k for k in ("cv", "notes", "preferences") if not ready[k]]
        log.warning("not scanning: set-up incomplete, missing %s (see the web app)", ", ".join(missing))
        return {"skipped": "not set up", "not_ready": missing}

    started = time.time()
    workdir = _new_workdir()
    run_id = db.start_run()
    stats: dict[str, Any] = {"run_id": run_id, "workdir": str(workdir), "skip_llm": skip_llm}
    log.info("run %d starting in %s", run_id, workdir)

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

        # -- profile -----------------------------------------------------
        digest = None
        if not skip_llm:
            digest = profile.load_digest(workdir)
            stats["profile"] = digest.headline

        criteria = criteria_hash(profile.profile_fingerprint())
        stats["criteria"] = criteria

        # -- fetch (persistent sources + ephemeral keyword queries) ------
        raw, fetch_stats = fetch.fetch_all(workdir, digest)
        stats.update(fetch_stats)

        if digest and settings().discovery.enabled:
            raw += _fetch_keyword_sources(digest, workdir, stats)

        # -- discovery channel A: harvest boards out of what we fetched --
        stats["harvested_sources"] = discovery.harvest(raw)

        # -- structure the prose sources ---------------------------------
        if not skip_llm:
            structured = extract.extract(raw, workdir)
            raw = [j for j in raw if not j.needs_extraction] + structured
            stats["extracted"] = len(structured)
        else:
            raw = [j for j in raw if not j.needs_extraction]

        # -- cheap local relevance gate ----------------------------------
        kept, pf_stats = fetch.prefilter_jobs(raw, digest)
        stats.update(pf_stats)

        # -- persist ------------------------------------------------------
        store_stats = fetch.store(kept, settings().limits.max_jobs_per_run)
        store_stats.pop("new_ids", None)
        stats.update(store_stats)

        # -- reasoning ----------------------------------------------------
        if not skip_llm and digest:
            stats.update(triage.run_triage(digest, criteria, workdir, run_id))
            stats.update(deepdive.run_deepdive(digest, criteria, workdir, run_id))
            stats["websearch"] = discovery.run_websearch(digest, workdir)

        stats["duration_seconds"] = round(time.time() - started, 1)
        digest_path = digest_mod.write_digest(workdir, run_id, criteria, stats)
        stats["digest"] = str(digest_path)

        db.finish_run(run_id, status, stats)
        log.info("run %d finished (%s) in %.1fs: %s", run_id, status, stats["duration_seconds"], stats)
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
        _run_lock.release()


def _fetch_keyword_sources(digest, workdir: Path, stats: dict[str, Any]) -> list[RawJob]:
    """Channel B: per-run keyword queries. Failures are non-fatal."""
    out: list[RawJob] = []
    found: dict[str, Any] = {}
    for cfg in discovery.keyword_sources(digest):
        try:
            jobs = build(dict(cfg, _workdir=str(workdir))).fetch()
            out.extend(jobs)
            found[cfg["id"]] = len(jobs)
        except Exception as exc:
            found[cfg["id"]] = f"{type(exc).__name__}: {exc}"
            log.warning("keyword source %s failed: %s", cfg["id"], exc)
    stats["keyword_queries"] = found
    stats["keyword_raw"] = len(out)
    return out
