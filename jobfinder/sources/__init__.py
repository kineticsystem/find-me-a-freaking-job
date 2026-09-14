"""Source adapters. Deterministic fetchers first, LLM-driven ones last."""

from __future__ import annotations

from typing import Any

from ..models import RawJob
from .base import REGISTRY, Source, register
from . import (  # noqa: F401  (import for the side effect of registering)
    aggregators,
    ashby,
    arbeitnow,
    greenhouse,
    hn_hiring,
    lever,
    llm_explorer,
    remoteok,
    weworkremotely,
)

__all__ = ["REGISTRY", "Source", "register", "build", "RawJob"]


def build(cfg: dict[str, Any]) -> Source:
    stype = cfg.get("type", "")
    if stype not in REGISTRY:
        raise KeyError(f"unknown source type {stype!r} (have: {sorted(REGISTRY)})")
    return REGISTRY[stype](cfg)
