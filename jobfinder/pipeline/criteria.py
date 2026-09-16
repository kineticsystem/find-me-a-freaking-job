"""What "already evaluated" means.

An evaluation is only valid for the profile + preferences it was made under.
Change either and every job becomes eligible for re-scoring, automatically.
"""

from __future__ import annotations

import hashlib

from ..config import DEFAULT_USER_ID


def criteria_hash(source_hash: str, preferences_fingerprint: str) -> str:
    blob = f"{source_hash}|{preferences_fingerprint}"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def current_criteria_hash(user_id: int = DEFAULT_USER_ID) -> str:
    from .profile import load

    return load(user_id).criteria
