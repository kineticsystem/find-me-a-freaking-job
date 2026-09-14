"""Hacker News "Ask HN: Who is hiring?" — unstructured prose, so each comment
is emitted with needs_extraction=True and handed to the LLM extractor."""

from __future__ import annotations

from .. import db
from ..models import RawJob
from ..textutil import html_to_text
from .base import Source, fetch_url, http_client, register

SEARCH = ("https://hn.algolia.com/api/v1/search_by_date"
          "?tags=story,author_whoishiring&query=who%20is%20hiring&hitsPerPage=3")
ITEM = "https://hn.algolia.com/api/v1/items/{id}"


@register("hn_hiring")
class HNHiring(Source):
    def fetch(self) -> list[RawJob]:
        limit = int(self.cfg.get("max_comments", 120))
        with http_client() as client:
            hits = fetch_url(SEARCH, client=client).json().get("hits", [])
            story = next(
                (h for h in hits if "who is hiring" in (h.get("title") or "").lower()),
                None,
            )
            if not story:
                return []
            thread = fetch_url(ITEM.format(id=story["objectID"]), client=client).json()

        # Extraction costs an LLM session per batch, so each comment is only ever
        # structured once: a two-hourly run picks up new replies, not the whole
        # thread again.
        out: list[RawJob] = []
        for child in (thread.get("children") or []):
            if len(out) >= limit:
                break
            text = html_to_text(child.get("text") or "")
            if len(text) < 80:
                continue  # "great thread!" noise
            key = f"hn:{child.get('id')}"
            if db.seen_discovery(key):
                continue
            db.mark_discovery(key, "hn_comment", str(child.get("id")))
            out.append(
                self._job(
                    description=text,
                    url=f"https://news.ycombinator.com/item?id={child.get('id')}",
                    posted_at=child.get("created_at"),
                    needs_extraction=True,
                )
            )
        return out
