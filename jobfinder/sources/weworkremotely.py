"""WeWorkRemotely category RSS feeds."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from ..models import RawJob
from ..textutil import find_salary, html_to_text
from .base import Source, fetch_url, register

DEFAULT_FEED = "https://weworkremotely.com/categories/remote-programming-jobs.rss"


@register("weworkremotely")
class WeWorkRemotely(Source):
    def fetch(self) -> list[RawJob]:
        feed = self.cfg.get("feed") or DEFAULT_FEED
        xml = fetch_url(feed).text
        root = ET.fromstring(xml)
        out: list[RawJob] = []
        for item in root.iter("item"):
            get = lambda tag: (item.findtext(tag) or "").strip()  # noqa: E731
            raw_title = get("title")
            # WWR titles look like "Company: Senior Backend Engineer"
            company, _, title = raw_title.partition(":")
            if not title:
                company, title = "", raw_title
            desc = html_to_text(get("description"))
            out.append(
                self._job(
                    company=company.strip(),
                    title=title.strip(),
                    location=get("region") or "Remote",
                    url=get("link"),
                    apply_url=get("link"),
                    description=desc,
                    posted_at=get("pubDate") or None,
                    remote_type="remote",
                    salary_raw=find_salary(desc),
                )
            )
        return out
