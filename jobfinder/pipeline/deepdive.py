"""Stage 2: one session per promising job, with the full posting."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .. import db
from ..config import settings
from ..models import DeepDive, ProfileDigest
from ..opencode import OpencodeError, run_session
from ..prompts import deepdive_prompt
from ..textutil import truncate
from . import progress

log = logging.getLogger(__name__)


def run_deepdive(digest: ProfileDigest, criteria: str, workdir: Path, run_id: int) -> dict[str, Any]:
    cfg = settings()
    rows = db.deepdive_candidates(
        criteria, cfg.limits.deepdive_min_score, cfg.limits.deepdive_top_n
    )
    stats: dict[str, Any] = {"deepdived": 0, "failed": 0, "strong": 0, "ineligible": 0}
    if not rows:
        return stats

    log.info("deep dive: %d candidates", len(rows))
    progress.stage("deepdive", f"reading the {len(rows)} best matches in full", total=len(rows))
    for i, row in enumerate(rows, start=1):
        started = time.time()
        job = {
            "company": row["company"], "title": row["title"],
            "location": row["location"] or "not stated", "url": row["url"],
        }
        posting = truncate(row["description"] or "", cfg.limits.posting_chars)
        try:
            result = run_session(
                deepdive_prompt(digest, job, posting),
                workdir / f"deepdive-{i:03d}",
                DeepDive,
                title=f"deepdive {row['company']} {row['title']}"[:60],
            )
        except OpencodeError as exc:
            stats["failed"] += 1
            log.warning("deep dive failed for job %s: %s", row["id"], exc)
            progress.unit_done(time.time() - started)
            continue
        progress.unit_done(time.time() - started, f"reading the {len(rows)} best matches in full")

        with db.connect() as conn:
            db.record_evaluation(
                conn,
                job_id=int(row["id"]),
                run_id=run_id,
                stage="deepdive",
                criteria_hash=criteria,
                score=result.score,
                verdict=result.verdict,
                eligible=result.eligible,
                summary=result.summary,
                eligibility=result.eligibility,
                salary=result.salary,
                tech_stack=result.tech_stack,
                concerns=result.concerns,
                rationale=result.rationale,
                model=cfg.opencode.model,
            )
        stats["deepdived"] += 1
        if result.verdict == "strong":
            stats["strong"] += 1
        if not result.eligible:
            stats["ineligible"] += 1

    return stats
