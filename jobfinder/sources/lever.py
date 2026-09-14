"""Lever postings API."""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import RawJob
from ..textutil import find_salary, html_to_text, infer_remote
from .base import Source, fetch_url, register

def _salary(posting: dict) -> str:
    rng = posting.get("salaryRange") or {}
    lo, hi = rng.get("min"), rng.get("max")
    if lo or hi:
        cur = rng.get("currency", "")
        return f"{cur} {lo or '?'} - {hi or '?'}".strip()
    return ""


API = "https://api.lever.co/v0/postings/{slug}?mode=json"


@register("lever")
class Lever(Source):
    def fetch(self) -> list[RawJob]:
        slug = self.cfg["slug"]
        data = fetch_url(API.format(slug=slug)).json()
        company = self.cfg.get("company") or slug.replace("-", " ").title()
        out: list[RawJob] = []
        for j in data if isinstance(data, list) else []:
            cats = j.get("categories") or {}
            desc = j.get("descriptionPlain") or html_to_text(j.get("description", ""))
            lists = "\n".join(
                f"{s.get('text','')}\n{html_to_text(s.get('content',''))}"
                for s in (j.get("lists") or [])
            )
            full = f"{desc}\n\n{lists}".strip()
            location = cats.get("location", "") or ""
            posted = j.get("createdAt")
            if isinstance(posted, (int, float)):
                posted = datetime.fromtimestamp(posted / 1000, timezone.utc).isoformat()
            out.append(
                self._job(
                    company=company,
                    title=j.get("text", ""),
                    location=location,
                    url=j.get("hostedUrl", ""),
                    apply_url=j.get("applyUrl") or j.get("hostedUrl", ""),
                    description=full,
                    posted_at=posted if isinstance(posted, str) else None,
                    tags=[v for v in (cats.get("commitment"), cats.get("team")) if v],
                    remote_type=infer_remote(location, cats.get("commitment", ""), full[:2000]),
                    salary_raw=_salary(j) or find_salary(full),
                )
            )
        return out
