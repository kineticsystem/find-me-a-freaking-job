"""Greenhouse job boards. Public, generous, no key required."""

from __future__ import annotations

from ..models import RawJob
from ..textutil import find_salary, html_to_text, infer_remote
from .base import Source, fetch_url, register

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


@register("greenhouse")
class Greenhouse(Source):
    def fetch(self) -> list[RawJob]:
        slug = self.cfg["slug"]
        data = fetch_url(API.format(slug=slug)).json()
        out: list[RawJob] = []
        company = self.cfg.get("company") or slug.replace("-", " ").title()
        for j in data.get("jobs", []):
            desc = html_to_text(j.get("content", ""))
            location = (j.get("location") or {}).get("name", "")
            out.append(
                self._job(
                    company=company,
                    title=j.get("title", ""),
                    location=location,
                    url=j.get("absolute_url", ""),
                    apply_url=j.get("absolute_url", ""),
                    description=desc,
                    posted_at=j.get("updated_at") or j.get("first_published"),
                    remote_type=infer_remote(location, j.get("title", ""), desc[:2000]),
                    salary_raw=find_salary(desc),
                )
            )
        return out
