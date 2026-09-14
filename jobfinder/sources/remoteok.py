"""RemoteOK public feed. First element is a legal notice, not a job."""

from __future__ import annotations

from ..models import RawJob
from ..textutil import find_salary, html_to_text
from .base import Source, fetch_url, register

API = "https://remoteok.com/api"


@register("remoteok")
class RemoteOK(Source):
    def fetch(self) -> list[RawJob]:
        data = fetch_url(self.cfg.get("url") or API).json()
        out: list[RawJob] = []
        for j in data:
            if not isinstance(j, dict) or not j.get("position"):
                continue  # legal notice / malformed entry
            desc = html_to_text(j.get("description", ""))
            salary = ""
            if j.get("salary_min") or j.get("salary_max"):
                salary = f"USD {j.get('salary_min') or '?'} - {j.get('salary_max') or '?'}"
            out.append(
                self._job(
                    company=j.get("company", ""),
                    title=j.get("position", ""),
                    location=j.get("location") or "Remote",
                    url=j.get("url", ""),
                    apply_url=j.get("apply_url") or j.get("url", ""),
                    description=desc,
                    posted_at=j.get("date"),
                    tags=[str(t) for t in (j.get("tags") or [])][:12],
                    remote_type="remote",
                    salary_raw=salary or find_salary(desc),
                )
            )
        return out
