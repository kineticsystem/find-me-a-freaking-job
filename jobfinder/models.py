"""Data shapes moving through the pipeline."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

RemoteType = Literal["remote", "hybrid", "onsite", "unknown"]


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = re.sub(r"\b(senior|sr|junior|jr|staff|principal|lead|ii|iii|iv|i)\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def fingerprint(company: str, title: str, location: str) -> str:
    """Stable identity for a posting so the same job on three boards collapses."""
    key = f"{_norm(company)}|{_norm(title)}|{_norm(location)[:40]}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


class RawJob(BaseModel):
    """What a source adapter emits, before normalisation."""

    source_id: str
    company: str = ""
    title: str = ""
    location: str = ""
    url: str = ""
    apply_url: str = ""
    description: str = ""
    salary_raw: str = ""
    posted_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    remote_type: RemoteType = "unknown"
    # Set when the payload is prose that the LLM must turn into postings
    # (HN "who is hiring" comments, scraped pages).
    needs_extraction: bool = False

    @field_validator("company", "title", "location", "url", "apply_url", "salary_raw", mode="before")
    @classmethod
    def _blank(cls, v: object) -> str:
        return "" if v is None else str(v)


class NormalizedJob(BaseModel):
    fingerprint: str
    source_id: str
    company: str
    title: str
    location: str
    remote_type: RemoteType
    url: str
    apply_url: str
    description: str
    salary_raw: str = ""
    posted_at: str | None = None
    tags: list[str] = Field(default_factory=list)


class TriageResult(BaseModel):
    """One entry of the model's batch verdict."""

    ref: int
    score: int
    verdict: Literal["strong", "maybe", "reject"]
    reason: str = ""

    @field_validator("score", mode="before")
    @classmethod
    def _clamp(cls, v: object) -> int:
        try:
            return max(0, min(100, int(float(str(v)))))
        except (TypeError, ValueError):
            return 0


class TriageBatch(BaseModel):
    # Every batch has at least one posting, so an empty answer is a failure to
    # answer, not a verdict.
    results: list[TriageResult] = Field(min_length=1)


class DeepDive(BaseModel):
    score: int = 0
    verdict: Literal["strong", "maybe", "reject"] = "maybe"
    summary: str = ""
    eligibility: str = ""
    eligible: bool = True
    salary: str = ""
    tech_stack: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    rationale: str = ""

    @field_validator("score", mode="before")
    @classmethod
    def _clamp(cls, v: object) -> int:
        try:
            return max(0, min(100, int(float(str(v)))))
        except (TypeError, ValueError):
            return 0


class ExtractedJob(BaseModel):
    company: str = ""
    title: str = ""
    location: str = ""
    url: str = ""
    description: str = ""
    salary_raw: str = ""
    remote_type: RemoteType = "unknown"


class ExtractionResult(BaseModel):
    jobs: list[ExtractedJob] = Field(default_factory=list)


class SearchQueries(BaseModel):
    queries: list[str] = Field(default_factory=list)


class ProfileDigest(BaseModel):
    """The distilled CV. Injected into every reasoning session, so keep it small.

    The substantive fields are required and non-empty: a digest with nothing in
    it is a failed distillation, and caching one once poisoned every score
    until it was noticed.
    """

    headline: str = Field(min_length=3)
    years_experience: str = ""
    seniority: str = ""
    core_skills: list[str] = Field(min_length=1)
    secondary_skills: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    recent_roles: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=40)
    search_keywords: list[str] = Field(min_length=3)

    def as_prompt_block(self) -> str:
        return self.model_dump_json(indent=2)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
