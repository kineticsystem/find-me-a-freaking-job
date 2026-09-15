"""Discovery: finding postings by means nobody hardcoded.

Four independent channels, deliberately redundant so that one being blocked
does not blind the system:

  A. HARVEST  - every posting fetched is scanned for Greenhouse / Lever / Ashby
                links. Recognised company boards are registered as permanent
                sources, so today's aggregator hit becomes tomorrow's direct,
                complete, unrate-limited feed. Self-reinforcing and offline.
  B. KEYWORD  - the model turns the CV into search terms which are pushed
                through the aggregator APIs that honour filters (Jobicy).
                Ephemeral: these are queries, not permanent sources.
  C. WEBSEARCH- classic search-engine scraping for ATS URLs. Best-effort: the
                free engines captcha-gate aggressively, so this is allowed to
                return nothing without failing the run.
  D. EXPLORER - the LLM browses an arbitrary site (config type llm_explorer).

Channel A is the one that always works, and it is the one that compounds.
"""

from __future__ import annotations

import logging
import re
import urllib.parse as urlparse
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from . import db
from .config import settings
from .models import RawJob, SearchQueries
from .opencode import OpencodeError, run_session
from .prompts import queries_prompt
from .sources.base import fetch_url

if TYPE_CHECKING:
    from .pipeline.profile import Candidate

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# ATS recognition
# --------------------------------------------------------------------------
ATS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"https?://boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"https?://api\.lever\.co/v0/postings/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"https?://api\.ashbyhq\.com/posting-api/job-board/([a-z0-9_.-]+)", re.I)),
    ("lever", re.compile(r"https?://jobs\.(?:eu\.)?lever\.co/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I)),
    # <tenant>.wd<n>.myworkdayjobs.com/[<lang>/]<site>; the API path (/wday/...)
    # and job pages (/<site>/job/...) both start with the site.
    ("workday", re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[a-z]{2}/)?([A-Za-z0-9_-]+)", re.I)),
]

SLUG_BLOCKLIST = {"embed", "www", "api", "jobs", "boards", "job-boards", "search", "static", "job"}


WORKDAY_NOT_SITES = {"wday", "job", "jobs", "login", "en-us"}


def detect_ats(url: str) -> tuple[str, str] | None:
    for stype, rx in ATS_PATTERNS:
        m = rx.search(url or "")
        if not m:
            continue
        if stype == "workday":
            tenant, wd, site = m.group(1).lower(), m.group(2).lower(), m.group(3)
            if site.lower() in WORKDAY_NOT_SITES:
                continue
            return stype, f"{tenant}/{wd}/{site}"
        slug = m.group(1).strip("/").lower()
        if slug and slug not in SLUG_BLOCKLIST and len(slug) > 1:
            return stype, slug
    return None


def source_config_for(stype: str, slug: str, url: str = "") -> dict:
    """The registry row for a detected board."""
    if stype == "workday":
        tenant, wd, site = slug.split("/")
        return {"id": f"wd-{tenant}", "type": "workday", "tenant": tenant, "wd": wd, "site": site,
                "enabled": True, "company": site.replace("_", " ").replace("-", " "), "added_from": url}
    return {"id": f"{stype[:2]}-{slug}", "type": stype, "slug": slug, "enabled": True,
            "company": slug.replace("-", " ").title(), "added_from": url}


def sniff_ats(url: str) -> tuple[str, str] | None:
    """Find the ATS behind a company careers page. First the raw HTML (an
    embedded board or a link), then, if the listings are built by
    JavaScript, a headless render: the page fetches its jobs from the ATS
    API, and that request names it."""
    from .render import _plain, render

    try:
        html_page = _plain(url)
        for _, href in html_page.links:
            if (hit := detect_ats(href)):
                return hit
        for m in re.finditer(r"https?://[^\s\"'<>)]+", html_page.text):
            if (hit := detect_ats(m.group(0))):
                return hit
    except Exception as exc:  # noqa: BLE001
        log.info("plain fetch of %s failed (%s); rendering", url, exc)

    page = render(url)
    for req in page.requests:
        if (hit := detect_ats(req)):
            return hit
    for _, href in page.links:
        if (hit := detect_ats(href)):
            return hit
    return probe_ats_by_name(url)


ATS_PROBES = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


def probe_ats_by_name(url: str) -> tuple[str, str] | None:
    """Try the company's domain name as the board slug on each ATS. Boards
    are almost always named after the company (acme.com -> "acme"), and a
    page can embed one without ever calling it while rendering."""
    import httpx

    from .config import settings

    host = re.sub(r"^www\.", "", urlparse.urlparse(url).netloc.lower())
    name = host.split(".")[0]
    # Up to nine quick requests; a board that does not answer in a few
    # seconds is not the one we are looking for.
    client = httpx.Client(timeout=6, follow_redirects=True, headers={"User-Agent": settings().http.user_agent})
    for slug in dict.fromkeys([name, name.replace("-", ""), name.replace("-", "_")]):
        if len(slug) < 3:
            continue
        for stype, template in ATS_PROBES.items():
            try:
                resp = client.get(template.format(slug=slug))
                resp.raise_for_status()
                data = resp.json()
            except Exception:
                continue
            jobs = data.get("jobs") if isinstance(data, dict) else data
            if isinstance(jobs, list) and jobs:
                log.info("ATS probe: %s is a %s board (%d openings)", slug, stype, len(jobs))
                return stype, slug
    return None


def _register(stype: str, slug: str, via: str, budget: list[int], followers: Iterable[int]) -> str | None:
    """Register one discovered board, followed by `followers` -- the people
    who follow whatever it was found through, so discovery never leaks one
    person's interests into another's list. `budget` is a one-element
    mutable counter."""
    if budget[0] <= 0:
        return None
    cfg = source_config_for(stype, slug) | {"discovered_from": via}
    source_id = cfg["id"]
    if db.seen_discovery(f"source:{source_id}"):
        return None
    db.mark_discovery(f"source:{source_id}", "source", via)
    if db.upsert_source(cfg, origin="discovered", followers=list(followers)):
        budget[0] -= 1
        log.info("discovered %s board %r (via %s)", stype, slug, via)
        return source_id
    return None


# --------------------------------------------------------------------------
# A. harvest
# --------------------------------------------------------------------------
def harvest(jobs: Iterable[RawJob]) -> list[str]:
    """Mine already-fetched postings for company ATS boards."""
    cfg = settings().discovery
    if not cfg.enabled:
        return []
    budget = [cfg.max_new_boards_per_run]
    found: list[str] = []
    followers_of: dict[str, list[int]] = {}
    for job in jobs:
        if budget[0] <= 0:
            break
        if job.source_id not in followers_of:
            followers_of[job.source_id] = db.source_followers(job.source_id)
        heirs = followers_of[job.source_id]
        for candidate in (job.apply_url, job.url):
            hit = detect_ats(candidate)
            if hit:
                sid = _register(*hit, via=f"harvest:{job.source_id}", budget=budget, followers=heirs)
                if sid:
                    found.append(sid)
                break
        else:
            # Links are often only in the body (HN comments, aggregator text).
            for match in re.finditer(r"https?://[^\s\"'<>)]+", job.description[:8000]):
                hit = detect_ats(match.group(0))
                if hit:
                    sid = _register(*hit, via=f"harvest-body:{job.source_id}", budget=budget, followers=heirs)
                    if sid:
                        found.append(sid)
                    break
    return found


# --------------------------------------------------------------------------
# B. keyword queries against filterable aggregators
# --------------------------------------------------------------------------
JOBICY_GEOS = {
    "poland": "europe", "germany": "europe", "france": "europe", "spain": "europe",
    "eu": "europe", "europe": "europe", "us": "usa", "usa": "usa",
    "united states": "usa", "uk": "uk", "canada": "canada",
}


def keyword_sources(cand: Candidate, limit: int = 6) -> list[dict[str, Any]]:
    """Query sources built from one user's CV vocabulary. Registered as
    sources (origin 'keyword') followed by that user alone, so they show in
    their list, can be switched off, and boards harvested from their results
    are theirs; fetched per user by the keyword step (plan, Decision 4)."""
    prefs, digest = cand.prefs, cand.digest
    assert digest is not None
    terms: list[str] = []
    for term in (digest.core_skills + digest.search_keywords + prefs.must_have):
        t = re.sub(r"[^a-z0-9+#.-]", "", str(term).lower())
        # Jobicy rejects very short tags with a 400.
        if 2 < len(t) <= 20 and t not in terms:
            terms.append(t)

    geos: list[str] = []
    for rule in prefs.location_rules:
        key = (rule.country or rule.region or "").lower()
        geo = JOBICY_GEOS.get(key)
        if geo and geo not in geos:
            geos.append(geo)
    geos = geos or ["anywhere"]

    out: list[dict[str, Any]] = []
    for term in terms[:limit]:
        geo = geos[len(out) % len(geos)]
        out.append({
            "id": f"kw-{term}-{geo}",
            "type": "jobicy",
            "tag": term,
            "geo": geo,
            "count": 50,
            "enabled": True,
            "company": f"search: {term} · {geo}",
        })
    return out


# --------------------------------------------------------------------------
# C. web search (best effort)
# --------------------------------------------------------------------------
SEARCH_ENDPOINTS = [
    "https://html.duckduckgo.com/html/?q={q}",
    "https://lite.duckduckgo.com/lite/?q={q}",
    "https://search.marcia.cc/search?q={q}",
]

_HREF_RX = re.compile(r'href="(/l/\?[^"]*uddg=[^"]+|https?://[^"]+)"', re.I)


def _decode(href: str) -> str:
    if "uddg=" in href:
        qs = urlparse.parse_qs(urlparse.urlparse(href).query)
        if qs.get("uddg"):
            return urlparse.unquote(qs["uddg"][0])
    return href


def web_search(query: str, limit: int) -> list[str]:
    """Return result URLs. Never raises; returns [] when engines block us."""
    extra = settings().discovery.search_endpoint
    endpoints = ([extra] if extra else []) + SEARCH_ENDPOINTS
    for template in endpoints:
        try:
            html = fetch_url(template.format(q=urlparse.quote_plus(query))).text
        except Exception as exc:
            log.debug("search endpoint failed: %s", exc)
            continue
        urls: list[str] = []
        for match in _HREF_RX.findall(html):
            link = _decode(match)
            if link.startswith("http") and "duckduckgo.com" not in link and link not in urls:
                urls.append(link)
            if len(urls) >= limit:
                break
        if urls:
            return urls
    return []


def _previous_queries() -> list[str]:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT detail FROM discovery_log WHERE kind='query' ORDER BY rowid DESC LIMIT 60"
        ).fetchall()
    return [r["detail"] for r in rows]


def _fallback_queries(cand: Candidate) -> list[str]:
    out = []
    for title in (cand.prefs.titles[:3] or ["Software Engineer"]):
        out += [
            f'site:boards.greenhouse.io "{title}" remote',
            f'site:jobs.lever.co "{title}" remote',
            f'site:jobs.ashbyhq.com "{title}" remote',
        ]
    return out


def generate_queries(cand: Candidate, workdir: Path, n: int) -> list[str]:
    tried = _previous_queries()
    queries: list[str] = []
    try:
        result = run_session(
            queries_prompt(cand, n, tried), workdir / f"queries-{cand.user_id}", SearchQueries,
            title="discovery queries",
        )
        queries = [q.strip() for q in result.queries if q and q.strip()]
    except OpencodeError as exc:
        log.warning("query generation failed (%s); falling back", exc)

    queries = queries or _fallback_queries(cand)
    fresh = [q for q in queries if q not in set(tried)]
    return (fresh or queries)[:n]


def run_websearch(cand: Candidate, workdir: Path) -> dict[str, Any]:
    cfg = settings().discovery
    stats: dict[str, Any] = {"queries": 0, "results": 0, "new_sources": []}
    if not cfg.enabled:
        return stats
    budget = [cfg.max_new_boards_per_run]

    for query in generate_queries(cand, workdir, cfg.queries_per_run):
        if budget[0] <= 0:
            break
        db.mark_discovery(f"query:{query}", "query", query)
        stats["queries"] += 1
        urls = web_search(query, cfg.results_per_query)
        stats["results"] += len(urls)
        for url in urls:
            hit = detect_ats(url)
            if hit:
                sid = _register(*hit, via=f"search:{query}"[:120], budget=budget, followers=[cand.user_id])
                if sid:
                    stats["new_sources"].append(sid)

    if stats["queries"] and not stats["results"]:
        stats["note"] = "search engines returned nothing (captcha-gated); relying on harvest"
    return stats


# --------------------------------------------------------------------------
def seed_from_config() -> int:
    """Load config/sources.yaml into the sources table. Idempotent."""
    from .config import seed_sources

    return sum(db.upsert_source(s.model_dump(), origin="config") for s in seed_sources())
