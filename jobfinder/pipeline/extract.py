"""Turn prose adverts (HN "who is hiring") into structured postings."""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import settings
from ..models import RawJob
from ..opencode import OpencodeError, estimate_tokens, run_session
from ..models import ExtractionResult
from ..prompts import extract_prompt
from ..textutil import clean, infer_remote, truncate

log = logging.getLogger(__name__)

# Extraction is bound by OUTPUT tokens, not input: each entry can yield several
# postings with a description each. Keep batches small and cap them per run --
# HN comments are remembered across runs, so coverage accumulates instead of
# being redone.
BATCH_TOKEN_BUDGET = 3000
PER_ENTRY_CHARS = 2000


def extract(jobs: list[RawJob], workdir: Path) -> list[RawJob]:
    """Replace needs_extraction jobs with structured ones."""
    pending = [j for j in jobs if j.needs_extraction]
    if not pending:
        return []

    max_batches = settings().limits.max_extract_batches
    out: list[RawJob] = []
    batch: list[dict[str, object]] = []
    used = 0

    def flush(index: int) -> None:
        nonlocal batch, used
        if not batch:
            return
        try:
            result = run_session(
                extract_prompt(batch),
                workdir / f"extract-{index}",
                ExtractionResult,
                title=f"extract batch {index}",
            )
        except OpencodeError as exc:
            log.warning("extraction batch %d failed: %s", index, exc)
            batch, used = [], 0
            return
        source_id = pending[0].source_id
        for j in result.jobs:
            if not j.title:
                continue
            desc = clean(j.description)
            out.append(
                RawJob(
                    source_id=source_id,
                    company=clean(j.company) or "Unknown",
                    title=clean(j.title),
                    location=clean(j.location),
                    url=j.url,
                    apply_url=j.url,
                    description=desc,
                    salary_raw=clean(j.salary_raw),
                    remote_type=j.remote_type
                    if j.remote_type != "unknown"
                    else infer_remote(j.location, j.title, desc),
                )
            )
        batch, used = [], 0

    index = 0
    for ref, job in enumerate(pending, start=1):
        if index >= max_batches:
            log.info("extraction capped at %d batches this run", max_batches)
            break
        text = truncate(job.description, PER_ENTRY_CHARS)
        cost = estimate_tokens(text)
        if batch and used + cost > BATCH_TOKEN_BUDGET:
            index += 1
            flush(index)
        batch.append({"ref": ref, "url": job.url, "text": text})
        used += cost
    if index < max_batches:
        index += 1
        flush(index)

    log.info("extraction: %d prose entries -> %d structured postings", len(pending), len(out))
    return out
