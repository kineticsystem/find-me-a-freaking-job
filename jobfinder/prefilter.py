"""Cheap deterministic relevance gate.

Bulk aggregators return every job on earth. Sending "Medical Coder" to a 27B
model at two-hour intervals is a waste of the only scarce resource here
(inference time), so postings must clear a keyword bar before triage.

This is intentionally permissive: it removes the obviously-irrelevant, and
leaves every real judgement call to the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import preferences
from .models import ProfileDigest, RawJob

# Roles that share vocabulary with tech jobs but never match a software CV.
HARD_EXCLUDE = re.compile(
    r"\b(nurse|nursing|medical coder|caregiver|truck driver|cdl|dental|"
    r"phlebotom|therapist|barista|welder|electrician|plumber|janitor|"
    r"security guard|warehouse|forklift|insurance agent|real estate agent|"
    r"cashier|line cook|housekeep)\w*\b",
    re.I,
)

TECH_HINT = re.compile(
    r"\b(engineer|developer|programmer|architect|sre|devops|backend|back-end|"
    r"frontend|front-end|full[- ]?stack|software|platform|infrastructure|data|"
    r"machine learning|ml|ai|cloud|security|qa|sysadmin|python|java|golang|"
    r"kubernetes|typescript|rust|scala)\b",
    re.I,
)


@dataclass(slots=True)
class Decision:
    keep: bool
    reason: str
    score: float = 0.0


def _terms(digest: ProfileDigest | None) -> list[str]:
    prefs = preferences()
    raw = list(prefs.titles) + list(prefs.must_have) + list(prefs.nice_to_have)
    if digest:
        raw += digest.search_keywords + digest.core_skills + digest.secondary_skills
    seen, out = set(), []
    for term in raw:
        t = str(term).strip().lower()
        if len(t) > 1 and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def evaluate(job: RawJob, digest: ProfileDigest | None = None) -> Decision:
    if job.needs_extraction:
        return Decision(True, "needs extraction", 1.0)  # judged after structuring

    title = job.title or ""
    haystack = f"{title}\n{job.location}\n{' '.join(job.tags)}\n{job.description[:3000]}"

    if not title.strip():
        return Decision(False, "no title")
    if HARD_EXCLUDE.search(title):
        return Decision(False, f"excluded occupation in title: {title[:60]}")

    terms = _terms(digest)
    hits = [t for t in terms if t in haystack.lower()]
    title_hits = [t for t in terms if t in title.lower()]

    if title_hits:
        return Decision(True, f"title matches {title_hits[:3]}", 2.0 + len(hits) * 0.1)
    if TECH_HINT.search(title):
        if hits:
            return Decision(True, f"tech title + {hits[:3]}", 1.0 + len(hits) * 0.1)
        return Decision(True, "tech title", 0.5)
    if len(hits) >= 3:
        return Decision(True, f"body matches {hits[:3]}", 0.4)
    return Decision(False, f"no relevance signal in {title[:60]!r}")


def apply(jobs: list[RawJob], digest: ProfileDigest | None = None) -> tuple[list[RawJob], list[tuple[RawJob, str]]]:
    """Split postings into (kept, [(dropped, reason)]), best-scoring first."""
    kept: list[tuple[float, RawJob]] = []
    dropped: list[tuple[RawJob, str]] = []
    for job in jobs:
        d = evaluate(job, digest)
        (kept.append((d.score, job)) if d.keep else dropped.append((job, d.reason)))
    kept.sort(key=lambda pair: pair[0], reverse=True)
    return [job for _, job in kept], dropped
