"""Turn prose adverts (HN "who is hiring") into structured postings."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from ..config import settings
from ..models import RawJob
from ..opencode import OpencodeError, SessionCancelled, estimate_tokens, run_session
from ..models import ExtractionResult
from ..prompts import extract_prompt
from ..textutil import clean, infer_remote, truncate
from . import progress

log = logging.getLogger(__name__)

# Extraction is bound by OUTPUT tokens, not input: each entry can yield several
# postings with a description each. Keep batches small and cap them per run --
# HN comments are remembered across runs, so coverage accumulates instead of
# being redone.
BATCH_TOKEN_BUDGET = 3000
PER_ENTRY_CHARS = 2000
# A rendered careers page is one entry that may list dozens of roles; it
# gets more room and a batch to itself.
PAGE_ENTRY_CHARS = 12500   # as_prompt_text's 12000 plus the entry header; never re-cuts the links


def extract(jobs: list[RawJob], workdir: Path) -> list[RawJob]:
    """Replace needs_extraction jobs with structured ones."""
    pending = [j for j in jobs if j.needs_extraction]
    if not pending:
        return []

    max_batches = settings().limits.max_extract_batches
    est_batches = min(max_batches, max(1, -(-sum(estimate_tokens(truncate(j.description, PER_ENTRY_CHARS)) for j in pending) // BATCH_TOKEN_BUDGET)))
    progress.stage("extract", f"structuring {len(pending)} free-text adverts", total=est_batches)
    out: list[RawJob] = []
    batch: list[dict[str, object]] = []
    used = 0

    def flush(index: int) -> None:
        nonlocal batch, used
        if not batch:
            return
        started = time.time()
        try:
            result = run_session(
                extract_prompt(batch),
                workdir / f"extract-{index}",
                ExtractionResult,
                title=f"extract batch {index}",
            )
        except SessionCancelled:
            batch, used = [], 0
            return
        except OpencodeError as exc:
            log.warning("extraction batch %d failed: %s", index, exc)
            progress.unit_done(time.time() - started)
            batch, used = [], 0
            return
        progress.unit_done(time.time() - started)
        # A batch is either one rendered page or prose entries from one source
        # (HN comments), so the first entry's source and company stand for all.
        source_id = str(batch[0]["source_id"])
        fallback_company = str(batch[0].get("company") or "")
        for j in result.jobs:
            if not j.title:
                continue
            desc = clean(j.description)
            out.append(
                RawJob(
                    source_id=source_id,
                    company=clean(j.company) or fallback_company or "Unknown",
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
        if progress.stop_requested():
            break
        if index >= max_batches:
            log.info("extraction capped at %d batches this run", max_batches)
            break
        is_page = job.source_id.startswith("web-") or job.source_id.startswith("explore-")
        text = truncate(job.description, PAGE_ENTRY_CHARS if is_page else PER_ENTRY_CHARS)
        cost = estimate_tokens(text)
        if batch and (is_page or used + cost > BATCH_TOKEN_BUDGET):
            index += 1
            flush(index)
        batch.append({"ref": ref, "url": job.url, "text": text, "source_id": job.source_id, "company": job.company})
        used += cost
        if is_page:                      # a page never shares a batch
            index += 1
            flush(index)
    if index < max_batches:
        index += 1
        flush(index)

    log.info("extraction: %d prose entries -> %d structured postings", len(pending), len(out))
    return out
