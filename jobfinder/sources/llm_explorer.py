"""A source with no API: hand the URL to opencode and let the model read it.

This is the escape hatch that keeps the system from being limited to boards
someone wrote an adapter for.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import ROOT, settings
from ..models import ExtractionResult, RawJob
from ..opencode import OpencodeError, run_session
from ..prompts import explore_prompt
from ..textutil import clean, infer_remote
from .base import Source, register

log = logging.getLogger(__name__)


@register("llm_explorer")
class LLMExplorer(Source):
    def fetch(self) -> list[RawJob]:
        url = self.cfg.get("url")
        if not url:
            return []
        workdir = Path(self.cfg.get("_workdir") or (ROOT / "runs" / "adhoc")) / f"explore-{self.id}"
        try:
            result = run_session(
                explore_prompt(url=url, hint=self.cfg.get("hint") or ""),
                workdir,
                ExtractionResult,
                agent=settings().opencode.explorer_agent,
                title=f"explore {self.id}",
            )
        except OpencodeError as exc:
            log.warning("explorer %s failed: %s", self.id, exc)
            return []

        out: list[RawJob] = []
        for j in result.jobs:
            if not j.title or not j.url:
                continue  # a posting you cannot open is worthless
            desc = clean(j.description)
            out.append(
                self._job(
                    company=clean(j.company),
                    title=clean(j.title),
                    location=clean(j.location),
                    url=j.url,
                    apply_url=j.url,
                    description=desc,
                    salary_raw=clean(j.salary_raw),
                    remote_type=j.remote_type
                    if j.remote_type != "unknown"
                    else infer_remote(j.location, j.title, desc[:2000]),
                )
            )
        return out
