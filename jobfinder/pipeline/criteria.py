"""What "already evaluated" means.

An evaluation is only valid for the profile + preferences it was made under.
Change either and every job becomes eligible for re-scoring, automatically.
"""

from __future__ import annotations

import hashlib

from ..config import preferences


def criteria_hash(profile_fingerprint: str) -> str:
    blob = f"{profile_fingerprint}|{preferences().fingerprint}"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def current_criteria_hash() -> str:
    from .profile import profile_fingerprint

    return criteria_hash(profile_fingerprint())
