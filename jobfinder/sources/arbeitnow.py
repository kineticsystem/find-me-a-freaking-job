"""Arbeitnow job board API (EU-heavy, generous, paginated)."""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import RawJob
from ..textutil import find_salary, html_to_text
from .base import Source, fetch_url, http_client, register

API = "https://www.arbeitnow.com/api/job-board-api"


@register("arbeitnow")
class Arbeitnow(Source):
    def fetch(self) -> list[RawJob]:
        pages = max(1, int(self.cfg.get("pages", 1)))
        out: list[RawJob] = []
        with http_client() as client:
            for page in range(1, pages + 1):
                data = fetch_url(f"{API}?page={page}", client=client).json()
                items = data.get("data") or []
                if not items:
                    break
                for j in items:
                    desc = html_to_text(j.get("description", ""))
                    created = j.get("created_at")
                    if isinstance(created, (int, float)):
                        created = datetime.fromtimestamp(created, timezone.utc).isoformat()
                    out.append(
                        self._job(
                            company=j.get("company_name", ""),
                            title=j.get("title", ""),
                            location=j.get("location", ""),
                            url=j.get("url", ""),
                            apply_url=j.get("url", ""),
                            description=desc,
                            posted_at=created if isinstance(created, str) else None,
                            tags=[str(t) for t in (j.get("tags") or [])][:12],
                            remote_type="remote" if j.get("remote") else "unknown",
                            salary_raw=find_salary(desc),
                        )
                    )
        return out
