"""Every prompt in one place, so the whole context budget is auditable."""

from __future__ import annotations

import json
from typing import Any, Sequence

from . import db
from .config import preferences, settings
from .models import ProfileDigest

_RULES = (
    "Write your answer ONLY to the file `result.json` in the current directory, "
    "using the write tool. It must be a single JSON object matching the schema "
    "exactly: no prose, no markdown fences, no extra keys."
)


def _market_block() -> str:
    """Geographic preference is an ordering, not a filter: a lower-priority
    market is still acceptable, it just loses a tie."""
    prefs = preferences()
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


def _rejections_block() -> str:
    """What the candidate has turned down, and why. Guidance, not rules: the
    aim is that the same kind of posting stops scoring well, not that any
    posting sharing a word with a rejected one is thrown out."""
    rejected = db.recent_rejections(settings().limits.rejections_in_prompt)
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


def _notes_block() -> str:
    """The candidate's own words, verbatim. The digest above is a summary of
    the CV; this is not summarised, so nuance ("knows X but is not a Y")
    reaches every judgement intact."""
    from .pipeline.profile import notes_text
    from .textutil import truncate

    notes = truncate(notes_text(), settings().limits.notes_chars)
    if not notes.strip():
        return ""
    return (
        "## THE CANDIDATE'S OWN NOTES (in their words; weigh these as heavily as the CV)\n"
        f"{notes}\n\n"
    )


def _context_block(digest: ProfileDigest) -> str:
    return (
        "## CANDIDATE PROFILE (distilled from their CV)\n"
        f"{digest.as_prompt_block()}\n\n"
        f"{_notes_block()}"
        "## SEARCH PREFERENCES (hard requirements and dealbreakers)\n"
        f"{preferences().as_prompt_block()}\n\n"
        f"{_market_block()}\n"
        f"{_rejections_block()}"
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
def triage_prompt(digest: ProfileDigest, jobs: Sequence[dict[str, Any]]) -> str:
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
    return f"""{_context_block(digest)}
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
def deepdive_prompt(digest: ProfileDigest, job: dict[str, Any], posting: str) -> str:
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
    return f"""{_context_block(digest)}
Analyse ONE posting in depth.

Pay particular attention to eligibility: the candidate lives in
{preferences().based_in or "their stated country"} and cannot relocate. A role
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
    return f"""Each entry below is a free-text job advert (typically a Hacker News
"Who is hiring?" comment). Convert them into structured postings.

Rules:
- One entry may contain several roles: emit one object per role.
- An entry that is not a job advert produces nothing.
- Copy facts only. Never invent a company, salary, or link. Unknown fields are
  the empty string.
- If the entry has no application link, use the source url given for it.

## ENTRIES
{body}

## SCHEMA
{json.dumps(_EXTRACT_SCHEMA, indent=2)}

{_RULES}
"""


def explore_prompt(url: str, hint: str) -> str:
    prefs = preferences()
    return f"""Find current job postings on this page and structure them.

## PAGE
{url}

## WHAT TO LOOK FOR
{hint or "Software engineering roles."}
Titles of interest: {", ".join(prefs.titles) or "software engineering"}
The candidate is based in {prefs.based_in or "Europe"} and needs remote-friendly roles.

Fetch the page, and follow at most one level of links into individual postings
if the listing page lacks detail. Do not crawl further. Return every relevant
posting you actually saw.

## SCHEMA
{json.dumps(_EXTRACT_SCHEMA, indent=2)}

{_RULES}
"""


# -------------------------------------------------------------- discovery ---
def queries_prompt(digest: ProfileDigest, n: int, already_tried: Sequence[str]) -> str:
    prefs = preferences()
    schema = {"queries": [f"str, {n} web search queries"]}
    tried = "\n".join(f"- {q}" for q in already_tried[-40:]) or "(none yet)"
    return f"""{_context_block(digest)}
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
