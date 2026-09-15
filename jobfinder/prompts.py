"""Every prompt in one place, so the whole context budget is auditable."""

from __future__ import annotations

import json
from typing import Any, Sequence

from typing import TYPE_CHECKING

from . import db
from .config import Preferences, settings
from .models import ProfileDigest

if TYPE_CHECKING:
    from .pipeline.profile import Candidate

_RULES = (
    "Write your answer ONLY to the file `result.json` in the current directory, "
    "using the write tool. It must be a single JSON object matching the schema "
    "exactly: no prose, no markdown fences, no extra keys."
)


def _market_block(prefs: Preferences) -> str:
    """Geographic preference is an ordering, not a filter: a lower-priority
    market is still acceptable, it just loses a tie."""
    order = prefs.market_priority or [
        r.country or r.region or "" for r in
        sorted(prefs.location_rules, key=lambda r: r.priority)
    ]
    order = [m for m in order if m]
    if not order:
        return ""
    ranked = " > ".join(f"{i}. {m}" for i, m in enumerate(order, start=1))
    return (
        "## MARKET PRIORITY\n"
        f"Search these markets in this order: {ranked}.\n"
        "This is a tie-breaker, not a filter. A role in a lower-priority market "
        "is still worth surfacing; it just loses to an equally good role in a "
        "higher-priority one. Where two postings are otherwise comparable, give "
        "the higher-priority market roughly 5-10 more points. Never inflate a "
        "weak role just because it sits in the top market, and never reject a "
        "strong one for sitting in the last.\n"
    )


def _rejections_block(user_id: int) -> str:
    """What the candidate has turned down, and why. Guidance, not rules: the
    aim is that the same kind of posting stops scoring well, not that any
    posting sharing a word with a rejected one is thrown out."""
    rejected = db.recent_rejections(settings().limits.rejections_in_prompt, user_id=user_id)
    if not rejected:
        return ""
    lines = "\n".join(
        f"- {r['title']} at {r['company']} ({r['location'] or 'location not stated'}"
        f"{', ' + r['remote_type'] if r['remote_type'] and r['remote_type'] != 'unknown' else ''}) "
        f"— \"{r['reason']}\""
        for r in rejected
    )
    return (
        "## POSTINGS THE CANDIDATE REJECTED, WITH THEIR REASONS\n"
        f"{lines}\n"
        "Treat these as guidance about taste, not as rules. A posting that "
        "would draw the same objection should score lower; a posting that merely "
        "resembles one of these in title or company is not thereby worse. Never "
        "reject a role only because it shares a keyword with a rejected one.\n\n"
    )


def _notes_block(notes_text: str) -> str:
    """The candidate's own words, verbatim. The digest above is a summary of
    the CV; this is not summarised, so nuance ("knows X but is not a Y")
    reaches every judgement intact."""
    from .textutil import truncate

    notes = truncate(notes_text, settings().limits.notes_chars)
    if not notes.strip():
        return ""
    return (
        "## THE CANDIDATE'S OWN NOTES (in their words; weigh these as heavily as the CV)\n"
        f"{notes}\n\n"
    )


def _context_block(cand: Candidate) -> str:
    assert cand.digest is not None, "context needs the digest"
    return (
        "## CANDIDATE PROFILE (distilled from their CV)\n"
        f"{cand.digest.as_prompt_block()}\n\n"
        f"{_notes_block(cand.notes)}"
        "## SEARCH PREFERENCES (hard requirements and dealbreakers)\n"
        f"{cand.prefs.as_prompt_block()}\n\n"
        f"{_market_block(cand.prefs)}\n"
        f"{_rejections_block(cand.user_id)}"
    )


# ---------------------------------------------------------------- profile ---
def profile_prompt(cv_text: str, notes: str) -> str:
    schema = {
        "headline": "str", "years_experience": "str", "seniority": "str",
        "core_skills": ["str"], "secondary_skills": ["str"], "domains": ["str"],
        "recent_roles": ["str, 'Title at Company (dates)'"], "languages": ["str"],
        "summary": "str, at most 120 words",
        "search_keywords": ["str, 10-20 terms you would type into a job board"],
    }
    return f"""Distil this CV into a compact structured profile.

It will be injected into every later reasoning step, so it must be SHORT and
factual. Do not embellish, do not infer skills that are not evidenced, and do
not include contact details or any personal data beyond professional facts.

## CV
{cv_text}

## THE CANDIDATE'S OWN NOTES ABOUT WHAT THEY WANT
{notes or "(none provided)"}

## SCHEMA
{json.dumps(schema, indent=2)}

{_RULES}
"""


# ----------------------------------------------------------------- triage ---
def triage_prompt(cand: Candidate, jobs: Sequence[dict[str, Any]]) -> str:
    listing = "\n\n".join(
        f"### ref {j['ref']}\n"
        f"company: {j['company']}\ntitle: {j['title']}\n"
        f"location: {j['location']} (remote: {j['remote_type']})\n"
        f"salary: {j['salary'] or 'not stated'}\n"
        f"excerpt: {j['excerpt']}"
        for j in jobs
    )
    schema = {
        "results": [
            {"ref": "int, echo the ref exactly",
             "score": "int 0-100",
             "verdict": "'strong' | 'maybe' | 'reject'",
             "reason": "str, at most 25 words"}
        ]
    }
    return f"""{_context_block(cand)}
Score each posting below for this candidate. This is a fast first pass: be
decisive and harsh. Reject anything that violates a dealbreaker or that the
candidate plainly cannot take from their location.

Return exactly {len(jobs)} results, one per ref, no duplicates.

## POSTINGS
{listing}

## SCHEMA
{json.dumps(schema, indent=2)}

{_RULES}
"""


# --------------------------------------------------------------- deepdive ---
def deepdive_prompt(cand: Candidate, job: dict[str, Any], posting: str) -> str:
    schema = {
        "score": "int 0-100",
        "verdict": "'strong' | 'maybe' | 'reject'",
        "summary": "str, 2-3 sentences: what the role actually is",
        "eligibility": "str, 1-2 sentences: can this candidate legally and practically take it from where they live?",
        "eligible": "bool",
        "salary": "str, as stated, or '' if not stated",
        "tech_stack": ["str"],
        "concerns": ["str, at most 4 concrete red flags"],
        "rationale": "str, at most 60 words, why this score",
    }
    return f"""{_context_block(cand)}
Analyse ONE posting in depth.

Pay particular attention to eligibility: the candidate lives in
{cand.prefs.based_in or "their stated country"} and cannot relocate. A role
that requires local work authorisation elsewhere, or on-site presence, is not
eligible no matter how good the fit — set eligible=false and cap the score at 20.

## POSTING
company: {job['company']}
title: {job['title']}
location: {job['location']}
url: {job['url']}

{posting}

## SCHEMA
{json.dumps(schema, indent=2)}

{_RULES}
"""


# --------------------------------------------------------------- extract ----
_EXTRACT_SCHEMA = {
    "jobs": [
        {"company": "str", "title": "str", "location": "str",
         "url": "str, absolute link to apply", "description": "str, at most 40 words",
         "salary_raw": "str", "remote_type": "'remote' | 'hybrid' | 'onsite' | 'unknown'"}
    ]
}


def extract_prompt(blobs: Sequence[dict[str, Any]]) -> str:
    body = "\n\n".join(
        f"### entry {b['ref']} (source url: {b['url']})\n{b['text']}" for b in blobs
    )
    return f"""Each entry below is free text that may describe jobs: a Hacker News
"Who is hiring?" comment, or the visible text of a company's careers page
(with the page's links listed at the end). Convert them into structured
postings.

Rules:
- One entry may contain several roles: emit one object per role.
- An entry that lists no jobs produces nothing.
- Copy facts only. Never invent a company, salary, or link. Unknown fields are
  the empty string.
- For a careers page, the company is the page's owner; for each role pick
  the link from the LINKS list that leads to that role, else use the source url.
- If the entry has no application link, use the source url given for it.

## ENTRIES
{body}

## SCHEMA
{json.dumps(_EXTRACT_SCHEMA, indent=2)}

{_RULES}
"""


def explore_prompt(url: str, hint: str) -> str:
    """Runs at fetch time, which is shared: the titles and countries of every
    user, so a page is read once for everyone."""
    from .config import all_preferences

    everyone = all_preferences()
    titles = sorted({t for p in everyone for t in p.titles})
    based = sorted({p.based_in for p in everyone if p.based_in})
    return f"""Find current job postings on this page and structure them.

## PAGE
{url}

## WHAT TO LOOK FOR
{hint or "Software engineering roles."}
Titles of interest: {", ".join(titles) or "software engineering"}
The candidates are based in {", ".join(based) or "Europe"} and need remote-friendly roles.

Fetch the page, and follow at most one level of links into individual postings
if the listing page lacks detail. Do not crawl further. Return every relevant
posting you actually saw.

## SCHEMA
{json.dumps(_EXTRACT_SCHEMA, indent=2)}

{_RULES}
"""


# -------------------------------------------------------------- discovery ---
def queries_prompt(cand: Candidate, n: int, already_tried: Sequence[str]) -> str:
    prefs = cand.prefs
    schema = {"queries": [f"str, {n} web search queries"]}
    tried = "\n".join(f"- {q}" for q in already_tried[-40:]) or "(none yet)"
    return f"""{_context_block(cand)}
Generate {n} DIVERSE web search queries that will surface job postings this
candidate should see. These go to a normal web search engine.

Tactics to mix (do not use only one):
- ATS site searches, e.g. site:boards.greenhouse.io "Backend Engineer" remote
- site:jobs.lever.co and site:jobs.ashbyhq.com variants
- niche and regional job boards for their target countries
- "we are hiring" phrasing on company career pages
- specific technologies from the profile combined with "remote" and a country

Do NOT repeat any of these previously used queries:
{tried}

Each query must be a single line of plain search syntax, no numbering.

## SCHEMA
{json.dumps(schema, indent=2)}

{_RULES}
"""
