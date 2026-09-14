"""A careers page with no API: rendered in a headless browser, then handed
to the extraction stage as one free-text entry. The model turns the page's
text and links into postings, the same way it structures HN comments."""

from __future__ import annotations

import logging

from ..models import RawJob
from ..render import render
from .base import Source, register

log = logging.getLogger(__name__)


@register("webpage")
class WebPage(Source):
    def fetch(self) -> list[RawJob]:
        url = self.cfg.get("url")
        if not url:
            return []
        page = render(url)
        if len(page.text) < 200:
            log.warning("%s: page rendered to almost no text; nothing to extract", self.id)
            return []
        return [
            RawJob(
                source_id=self.id,
                company=self.cfg.get("company", ""),
                url=url,
                description=page.as_prompt_text(),
                needs_extraction=True,
            )
        ]
