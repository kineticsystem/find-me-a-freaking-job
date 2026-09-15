"""Fetch every active source concurrently, then normalise and store."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .. import db, prefilter
from ..config import settings
from ..models import NormalizedJob, ProfileDigest, RawJob, fingerprint
from ..sources import build
from ..textutil import clean, infer_remote, truncate

log = logging.getLogger(__name__)

MAX_DESC_CHARS = 60000  # what we keep in the DB, not what we send to the model


def fetch_all(workdir: Path, digest: ProfileDigest | None) -> tuple[list[RawJob], dict[str, Any]]:
    cfg = settings()
    sources = db.active_sources()
    stats: dict[str, Any] = {"sources_run": 0, "sources_failed": 0, "raw": 0, "per_source": {}}
    jobs: list[RawJob] = []

    def _run(scfg: dict[str, Any]) -> tuple[str, list[RawJob], str]:
        try:
            source = build(dict(scfg, _workdir=str(workdir)))
            return scfg["id"], source.fetch(), ""
        except Exception as exc:  # one bad board must not kill the run
            return scfg["id"], [], f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=cfg.http.max_concurrency) as pool:
        futures = [pool.submit(_run, s) for s in sources]
        for fut in as_completed(futures):
            source_id, found, error = fut.result()
            stats["sources_run"] += 1
            stats["per_source"][source_id] = len(found) if not error else error
            if error:
                stats["sources_failed"] += 1
                log.warning("source %s failed: %s", source_id, error)
            else:
                jobs.extend(found)
            db.record_source_result(source_id, len(found), error)

    stats["raw"] = len(jobs)
    return jobs, stats


def normalize(job: RawJob) -> NormalizedJob:
    company = clean(job.company) or "Unknown"
    title = clean(job.title)
    location = clean(job.location)
    desc = truncate(clean(job.description), MAX_DESC_CHARS)
    remote = job.remote_type
    if remote == "unknown":
        remote = infer_remote(location, title, desc[:3000])
    return NormalizedJob(
        fingerprint=fingerprint(company, title, location),
        source_id=job.source_id,
        company=company,
        title=title,
        location=location,
        remote_type=remote,  # type: ignore[arg-type]
        url=job.url,
        apply_url=job.apply_url or job.url,
        description=desc,
        salary_raw=clean(job.salary_raw),
        posted_at=job.posted_at,
        tags=[clean(t) for t in job.tags if t][:12],
    )


def store(jobs: list[RawJob], limit: int) -> dict[str, Any]:
    """Normalise, dedupe and persist. Returns counts."""
    new_ids: list[int] = []
    seen: set[str] = set()
    stored = 0
    with db.connect() as conn:
        for raw in jobs[:limit]:
            job = normalize(raw)
            if not job.title or job.fingerprint in seen:
                continue
            seen.add(job.fingerprint)
            job_id, is_new = db.upsert_job(conn, job)
            stored += 1
            if is_new:
                new_ids.append(job_id)
    return {"stored": stored, "new": len(new_ids), "new_ids": new_ids}


def prefilter_jobs(
    jobs: list[RawJob], digests: list[ProfileDigest]
) -> tuple[list[RawJob], dict[str, Any]]:
    """Keeps a posting that passes the gate for any user's vocabulary."""
    if not digests:
        kept, dropped = prefilter.apply(jobs, None)
        return kept, {"prefilter_kept": len(kept), "prefilter_dropped": len(dropped)}
    keep_ids: set[int] = set()
    for digest in digests:
        kept, _ = prefilter.apply(jobs, digest)
        keep_ids.update(id(j) for j in kept)
    kept = [j for j in jobs if id(j) in keep_ids]
    return kept, {"prefilter_kept": len(kept), "prefilter_dropped": len(jobs) - len(kept)}
