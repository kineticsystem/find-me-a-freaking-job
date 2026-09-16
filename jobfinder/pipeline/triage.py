"""Stage 1 of reasoning: cheap batch scoring of everything unseen."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .. import db
from ..config import settings
from ..models import TriageBatch
from .profile import Candidate
from ..opencode import OpencodeError, SessionCancelled, run_session
from ..prompts import triage_prompt
from ..textutil import truncate
from . import progress

log = logging.getLogger(__name__)

EXCERPT_CHARS = 700


def _row_to_entry(ref: int, row: Any) -> dict[str, Any]:
    return {
        "ref": ref,
        "company": row["company"],
        "title": row["title"],
        "location": row["location"] or "not stated",
        "remote_type": row["remote_type"],
        "salary": row["salary_raw"],
        "excerpt": truncate(row["description"] or "", EXCERPT_CHARS, note=" [...]"),
    }


def run_triage(cand: Candidate, workdir: Path, run_id: int) -> dict[str, Any]:
    criteria = cand.criteria
    cfg = settings()
    batch_size = cfg.limits.triage_batch_size
    pending = db.jobs_needing("triage", criteria, cfg.limits.max_jobs_per_run, user_id=cand.user_id)
    stats: dict[str, Any] = {"triaged": 0, "batches": 0, "failed_batches": 0, "strong": 0}
    if not pending:
        return stats

    n_batches = -(-len(pending) // batch_size)
    log.info("triage: %d jobs in %d batches", len(pending), n_batches)
    progress.stage("triage", f"scoring {len(pending)} postings", total=n_batches)

    for index in range(0, len(pending), batch_size):
        if progress.stop_requested():
            stats["stopped"] = True
            log.info("stop requested; ending %s here", "triage" if "triage" in __name__ else "deep dive")
            break
        chunk = pending[index : index + batch_size]
        by_ref = {i + 1: row for i, row in enumerate(chunk)}
        entries = [_row_to_entry(ref, row) for ref, row in by_ref.items()]
        batch_no = index // batch_size + 1
        stats["batches"] += 1
        started = time.time()

        try:
            result = run_session(
                triage_prompt(cand, entries),
                workdir / f"triage-{batch_no:03d}",
                TriageBatch,
                title=f"triage batch {batch_no}",
            )
        except SessionCancelled:
            stats["stopped"] = True
            break
        except OpencodeError as exc:
            stats["failed_batches"] += 1
            log.warning("triage batch %d failed: %s", batch_no, exc)
            progress.unit_done(time.time() - started)
            continue
        progress.unit_done(time.time() - started, f"scoring {len(pending)} postings")

        seen: set[int] = set()
        with db.connect() as conn:
            for item in result.results:
                row = by_ref.get(item.ref)
                if row is None or item.ref in seen:
                    continue  # hallucinated or duplicated ref
                seen.add(item.ref)
                db.record_evaluation(
                    conn,
                    job_id=int(row["id"]),
                    run_id=run_id,
                    stage="triage",
                    criteria_hash=criteria,
                    user_id=cand.user_id,
                    score=item.score,
                    verdict=item.verdict,
                    rationale=item.reason,
                    model=cfg.opencode.model,
                )
                stats["triaged"] += 1
                if item.verdict == "strong":
                    stats["strong"] += 1

        missing = set(by_ref) - seen
        if missing:
            log.warning("triage batch %d omitted refs %s", batch_no, sorted(missing))

    return stats
