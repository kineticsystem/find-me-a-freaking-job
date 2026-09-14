"""Ashby public job board API."""

from __future__ import annotations

from ..models import RawJob
from ..textutil import find_salary, html_to_text, infer_remote
from .base import Source, fetch_url, register

API = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


@register("ashby")
class Ashby(Source):
    def fetch(self) -> list[RawJob]:
        slug = self.cfg["slug"]
        data = fetch_url(API.format(slug=slug)).json()
        company = self.cfg.get("company") or data.get("name") or slug.title()
        out: list[RawJob] = []
        for j in data.get("jobs", []):
            desc = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", ""))
            location = j.get("location", "") or ""
            comp = j.get("compensation") or {}
            salary = ""
            if isinstance(comp, dict):
                salary = comp.get("compensationTierSummary") or ""
            out.append(
                self._job(
                    company=company,
                    title=j.get("title", ""),
                    location=location,
                    url=j.get("jobUrl", ""),
                    apply_url=j.get("applyUrl") or j.get("jobUrl", ""),
                    description=desc,
                    posted_at=j.get("publishedAt"),
                    tags=[t for t in (j.get("department"), j.get("team")) if t],
                    remote_type="remote" if j.get("isRemote") else infer_remote(location, desc[:2000]),
                    salary_raw=salary or find_salary(desc),
                )
            )
        return out
