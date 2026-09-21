"""What "already evaluated" means.

An evaluation is only valid for the profile + preferences it was made under,
by the model that made it. Change any of the three and every job becomes
eligible for re-scoring, automatically; the old scores stay in history and
show as stale until replaced. The model is part of it because two models
put the same postings on different scales (doc/score_comparison.md): a list
half scored by one and half by the other would not be sortable.
"""

from __future__ import annotations

import hashlib

from ..config import DEFAULT_USER_ID, settings


def criteria_hash(source_hash: str, preferences_fingerprint: str, model: str | None = None) -> str:
    blob = f"{source_hash}|{preferences_fingerprint}|{model or settings().llm.model}"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def current_criteria_hash(user_id: int = DEFAULT_USER_ID) -> str:
    from .profile import load

    return load(user_id).criteria
