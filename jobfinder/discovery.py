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
from typing import Any, Iterable

from . import db
from .config import preferences, settings
from .models import ProfileDigest, RawJob, SearchQueries
from .opencode import OpencodeError, run_session
from .prompts import queries_prompt
from .sources.base import fetch_url

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# ATS recognition
# --------------------------------------------------------------------------
ATS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"https?://jobs\.(?:eu\.)?lever\.co/([a-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([a-z0-9_.-]+)", re.I)),
]

SLUG_BLOCKLIST = {"embed", "www", "api", "jobs", "boards", "job-boards", "search", "static", "job"}


def detect_ats(url: str) -> tuple[str, str] | None:
    for stype, rx in ATS_PATTERNS:
        m = rx.search(url or "")
        if m:
            slug = m.group(1).strip("/").lower()
            if slug and slug not in SLUG_BLOCKLIST and len(slug) > 1:
                return stype, slug
    return None


def _register(stype: str, slug: str, via: str, budget: list[int]) -> str | None:
    """Register one discovered board. `budget` is a one-element mutable counter."""
    if budget[0] <= 0:
        return None
    source_id = f"{stype[:2]}-{slug}"
    if db.seen_discovery(f"source:{source_id}"):
        return None
    db.mark_discovery(f"source:{source_id}", "source", via)
    cfg = {
        "id": source_id, "type": stype, "slug": slug, "enabled": True,
        "company": slug.replace("-", " ").title(), "discovered_from": via,
    }
    if db.upsert_source(cfg, origin="discovered"):
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
    for job in jobs:
        if budget[0] <= 0:
            break
        for candidate in (job.apply_url, job.url):
            hit = detect_ats(candidate)
            if hit:
                sid = _register(*hit, via=f"harvest:{job.source_id}", budget=budget)
                if sid:
                    found.append(sid)
                break
        else:
            # Links are often only in the body (HN comments, aggregator text).
            for match in re.finditer(r"https?://[^\s\"'<>)]+", job.description[:8000]):
                hit = detect_ats(match.group(0))
                if hit:
                    sid = _register(*hit, via=f"harvest-body:{job.source_id}", budget=budget)
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


def keyword_sources(digest: ProfileDigest, limit: int = 6) -> list[dict[str, Any]]:
    """Ephemeral per-run query sources built from the CV's own vocabulary."""
    prefs = preferences()
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
            "_ephemeral": True,
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


def _fallback_queries() -> list[str]:
    out = []
    for title in (preferences().titles[:3] or ["Software Engineer"]):
        out += [
            f'site:boards.greenhouse.io "{title}" remote',
            f'site:jobs.lever.co "{title}" remote',
            f'site:jobs.ashbyhq.com "{title}" remote',
        ]
    return out


def generate_queries(digest: ProfileDigest, workdir: Path, n: int) -> list[str]:
    tried = _previous_queries()
    queries: list[str] = []
    try:
        result = run_session(
            queries_prompt(digest, n, tried), workdir / "queries", SearchQueries,
            title="discovery queries",
        )
        queries = [q.strip() for q in result.queries if q and q.strip()]
    except OpencodeError as exc:
        log.warning("query generation failed (%s); falling back", exc)

    queries = queries or _fallback_queries()
    fresh = [q for q in queries if q not in set(tried)]
    return (fresh or queries)[:n]


def run_websearch(digest: ProfileDigest, workdir: Path) -> dict[str, Any]:
    cfg = settings().discovery
    stats: dict[str, Any] = {"queries": 0, "results": 0, "new_sources": []}
    if not cfg.enabled:
        return stats
    budget = [cfg.max_new_boards_per_run]

    for query in generate_queries(digest, workdir, cfg.queries_per_run):
        if budget[0] <= 0:
            break
        db.mark_discovery(f"query:{query}", "query", query)
        stats["queries"] += 1
        urls = web_search(query, cfg.results_per_query)
        stats["results"] += len(urls)
        for url in urls:
            hit = detect_ats(url)
            if hit:
                sid = _register(*hit, via=f"search:{query}"[:120], budget=budget)
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
