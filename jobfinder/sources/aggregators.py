"""Bulk remote-job aggregators with open APIs.

These return everything (medical coders included), so the local pre-filter in
jobfinder/prefilter.py does the first cut before any tokens are spent.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..models import RawJob
from ..textutil import find_salary, html_to_text
from .base import Source, fetch_url, http_client, register


def _epoch(value: object) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat()  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _range(lo: object, hi: object, currency: str = "", period: str = "") -> str:
    if not lo and not hi:
        return ""
    tail = f" / {period}" if period else ""
    return f"{currency} {lo or '?'} - {hi or '?'}{tail}".strip()


@register("remotive")
class Remotive(Source):
    API = "https://remotive.com/api/remote-jobs"

    def fetch(self) -> list[RawJob]:
        params = {"limit": str(self.cfg.get("limit", 200))}
        if self.cfg.get("category"):
            params["category"] = self.cfg["category"]
        if self.cfg.get("search"):
            params["search"] = self.cfg["search"]
        data = fetch_url(self.API, params=params).json()
        out = []
        for j in data.get("jobs", []):
            desc = html_to_text(j.get("description", ""))
            out.append(
                self._job(
                    company=j.get("company_name", ""),
                    title=j.get("title", ""),
                    location=j.get("candidate_required_location", "") or "Remote",
                    url=j.get("url", ""),
                    apply_url=j.get("url", ""),
                    description=desc,
                    posted_at=j.get("publication_date"),
                    tags=[str(t) for t in (j.get("tags") or [])][:12],
                    remote_type="remote",
                    salary_raw=j.get("salary") or find_salary(desc),
                )
            )
        return out


@register("jobicy")
class Jobicy(Source):
    """The one aggregator here whose tag/geo filters actually work."""

    API = "https://jobicy.com/api/v2/remote-jobs"

    def fetch(self) -> list[RawJob]:
        params = {"count": str(self.cfg.get("count", 50))}
        for key in ("tag", "geo", "industry"):
            if self.cfg.get(key):
                params[key] = str(self.cfg[key])
        data = fetch_url(self.API, params=params).json()
        out = []
        for j in data.get("jobs", []):
            desc = html_to_text(j.get("jobDescription") or j.get("jobExcerpt") or "")
            out.append(
                self._job(
                    company=j.get("companyName", ""),
                    title=j.get("jobTitle", ""),
                    location=j.get("jobGeo", "") or "Remote",
                    url=j.get("url", ""),
                    apply_url=j.get("url", ""),
                    description=desc,
                    posted_at=j.get("pubDate"),
                    tags=list(j.get("jobIndustry") or []) + list(j.get("jobType") or []),
                    remote_type="remote",
                    salary_raw=_range(
                        j.get("salaryMin"), j.get("salaryMax"),
                        j.get("salaryCurrency", ""), j.get("salaryPeriod", ""),
                    ) or find_salary(desc),
                )
            )
        return out


@register("himalayas")
class Himalayas(Source):
    API = "https://himalayas.app/jobs/api"

    def fetch(self) -> list[RawJob]:
        params = {"limit": str(self.cfg.get("limit", 100))}
        data = fetch_url(self.API, params=params).json()
        out = []
        for j in data.get("jobs", []):
            desc = html_to_text(j.get("description", "")) or j.get("excerpt", "")
            locs = j.get("locationRestrictions") or []
            out.append(
                self._job(
                    company=j.get("companyName", ""),
                    title=j.get("title", ""),
                    location=", ".join(str(x) for x in locs) or "Remote",
                    url=j.get("guid") or j.get("applicationLink", ""),
                    apply_url=j.get("applicationLink") or j.get("guid", ""),
                    description=desc,
                    posted_at=_epoch(j.get("pubDate")),
                    tags=[str(c) for c in (j.get("categories") or [])][:12],
                    remote_type="remote",
                    salary_raw=_range(
                        j.get("minSalary"), j.get("maxSalary"),
                        j.get("currency", ""), j.get("salaryPeriod", ""),
                    ) or find_salary(desc),
                )
            )
        return out


@register("themuse")
class TheMuse(Source):
    API = "https://www.themuse.com/api/public/jobs"

    def fetch(self) -> list[RawJob]:
        pages = max(1, int(self.cfg.get("pages", 2)))
        category = self.cfg.get("category", "Software Engineering")
        out: list[RawJob] = []
        with http_client() as client:
            for page in range(1, pages + 1):
                data = fetch_url(
                    self.API,
                    client=client,
                    params={"category": category, "page": str(page)},
                ).json()
                for j in data.get("results", []):
                    desc = html_to_text(j.get("contents", ""))
                    locs = [l.get("name", "") for l in (j.get("locations") or [])]
                    refs = j.get("refs") or {}
                    out.append(
                        self._job(
                            company=(j.get("company") or {}).get("name", ""),
                            title=j.get("name", ""),
                            location=", ".join(locs),
                            url=refs.get("landing_page", ""),
                            apply_url=refs.get("landing_page", ""),
                            description=desc,
                            posted_at=j.get("publication_date"),
                            tags=[l.get("name", "") for l in (j.get("levels") or [])],
                            salary_raw=find_salary(desc),
                        )
                    )
        return out
