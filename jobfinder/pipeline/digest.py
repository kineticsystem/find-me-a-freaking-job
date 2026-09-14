"""Human-readable run report: runs/<ts>/digest.md."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import db
from ..config import settings
from ..models import utcnow


def _fmt_list(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return str(raw)
    return ", ".join(str(i) for i in items) if isinstance(items, list) else str(items)


def write_digest(workdir: Path, run_id: int, criteria: str, stats: dict[str, Any]) -> Path:
    cfg = settings()
    jobs, _ = db.list_jobs(min_score=cfg.limits.digest_min_score, limit=50, criteria_hash=criteria)

    lines = [
        f"# Job run #{run_id}",
        "",
        f"_{utcnow()}_",
        "",
        f"**{len(jobs)}** postings at or above score {cfg.limits.digest_min_score}.",
        "",
        "```",
        json.dumps(stats, indent=2)[:2000],
        "```",
        "",
    ]

    if not jobs:
        lines += ["Nothing cleared the bar this run.", ""]

    for job in jobs:
        score = job.get("score")
        head = f"## {score if score is not None else '--'} · {job['title']} — {job['company']}"
        lines.append(head)
        meta = [job.get("location") or "location not stated", job.get("remote_type") or "unknown"]
        if job.get("salary") or job.get("salary_raw"):
            meta.append(job.get("salary") or job.get("salary_raw"))
        if job.get("eligible") == 0:
            meta.append("**NOT ELIGIBLE**")
        lines.append(" · ".join(str(m) for m in meta if m))
        lines.append("")
        if job.get("summary"):
            lines += [job["summary"], ""]
        if job.get("eligibility"):
            lines += [f"*Eligibility:* {job['eligibility']}", ""]
        concerns = _fmt_list(job.get("concerns"))
        if concerns:
            lines += [f"*Concerns:* {concerns}", ""]
        if job.get("rationale"):
            lines += [f"*Why:* {job['rationale']}", ""]
        lines += [f"[Apply]({job.get('apply_url') or job.get('url')}) · source: `{job['source_id']}` · id `{job['id']}`", "", "---", ""]

    path = workdir / "digest.md"
    path.write_text("\n".join(lines))

    latest = settings().paths.resolve("runs") / "latest-digest.md"
    latest.write_text("\n".join(lines))
    return path
