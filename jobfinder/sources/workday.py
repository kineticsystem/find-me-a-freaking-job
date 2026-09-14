"""Workday career sites (<tenant>.wd<n>.myworkdayjobs.com/<site>).

The site's own JSON API: a paged POST for the list, a GET per posting for
the description. Widespread among larger companies; slower than the other
boards because the description is one request per job, so the number of
detail fetches per scan is capped.
"""

from __future__ import annotations

import logging

from ..models import RawJob
from ..textutil import find_salary, html_to_text, infer_remote
from .base import Source, http_client, register

log = logging.getLogger(__name__)

PAGE = 20
MAX_DETAILS = 150


@register("workday")
class Workday(Source):
    def fetch(self) -> list[RawJob]:
        tenant, wd, site = self.cfg["tenant"], self.cfg.get("wd", "wd1"), self.cfg["site"]
        base = f"https://{tenant}.{wd}.myworkdayjobs.com"
        api = f"{base}/wday/cxs/{tenant}/{site}"
        company = self.cfg.get("company") or site.replace("_", " ")
        out: list[RawJob] = []
        with http_client() as client:
            headers = {"accept": "application/json", "content-type": "application/json"}
            postings: list[dict] = []
            offset, total = 0, None
            while True:
                resp = client.post(f"{api}/jobs", json={"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": ""}, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                page = data.get("jobPostings") or []
                postings.extend(page)
                if total is None:                  # only the first page reports it
                    total = int(data.get("total") or 0)
                offset += PAGE
                if len(page) < PAGE or offset >= total or offset >= 1000:
                    break
            for j in postings[:MAX_DETAILS]:
                path = j.get("externalPath", "")
                title = j.get("title", "")
                if not path or not title:
                    continue
                desc, posted, url = "", None, f"{base}/{site}{path}"
                try:
                    info = client.get(f"{api}{path}", headers=headers).json().get("jobPostingInfo", {})
                    desc = html_to_text(info.get("jobDescription", ""))
                    posted = info.get("startDate") or None
                    url = info.get("externalUrl") or url
                except Exception as exc:  # noqa: BLE001 - keep the listing even without its body
                    log.debug("workday detail failed for %s: %s", path, exc)
                location = j.get("locationsText", "") or ""
                out.append(
                    self._job(
                        company=company, title=title, location=location, url=url, apply_url=url,
                        description=desc, posted_at=posted,
                        remote_type=infer_remote(location, title, desc[:2000]),
                        salary_raw=find_salary(desc),
                    )
                )
        return out
