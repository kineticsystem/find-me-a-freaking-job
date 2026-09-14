"""Render a page in a headless browser: for career pages that build their
listings in JavaScript, which is most of them.

Returns the visible text, the links on the page, and the URLs the page
fetched while rendering -- the last is how a hidden ATS gives itself away.
Playwright and Chromium are in the image; when they are missing (a dev
checkout) it degrades to a plain HTTP fetch of the raw HTML.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .textutil import clean

log = logging.getLogger(__name__)

RENDER_TIMEOUT_MS = 45_000
MAX_TEXT_CHARS = 60_000
MAX_LINKS = 150

_JOBBY = re.compile(r"job|career|position|opening|vacanc|role|apply|hiring|greenhouse|lever|ashby|workable|rippling|bamboohr|smartrecruiters|recruitee|teamtailor|breezy|workday|jobvite", re.I)


@dataclass
class Rendered:
    url: str
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)   # (anchor text, absolute href)
    requests: list[str] = field(default_factory=list)            # URLs fetched while rendering
    rendered: bool = False                                       # True if a browser was used

    def as_prompt_text(self, max_chars: int = 12_000) -> str:
        """What the extractor sees: the text, then the job-looking links so it
        can attach an apply URL to each role. The links get the last third of
        the budget, so a long page cannot push them out."""
        from .textutil import truncate

        links = [(t, h) for t, h in self.links if _JOBBY.search(h) or _JOBBY.search(t)] or self.links
        link_block = ""
        if links:
            link_block = "\n\nLINKS ON THE PAGE, in page order (row: link text -> url):\n" + "\n".join(f"- {t[:90]} -> {h}" for t, h in links[:80])
            link_block = truncate(link_block, max_chars // 3, note="\n[... more links ...]")
        return truncate(self.text, max_chars - len(link_block)) + link_block


def _plain(url: str) -> Rendered:
    from .sources.base import fetch_url
    from .textutil import html_to_text

    html = fetch_url(url).text
    links = [(clean(t), h) for h, t in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', html, re.I | re.S)]
    return Rendered(url=url, text=html_to_text(html)[:MAX_TEXT_CHARS], links=links[:MAX_LINKS], requests=[], rendered=False)


def render(url: str) -> Rendered:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("playwright not installed; fetching %s without rendering", url)
        return _plain(url)

    requests: list[str] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) jobfinder/0.1 (+personal job search)")
                page.on("request", lambda r: requests.append(r.url))
                page.goto(url, wait_until="networkidle", timeout=RENDER_TIMEOUT_MS)
                text = page.evaluate("() => document.body ? document.body.innerText : ''")
                # Label each link with its row's first line, so "View & Apply"
                # becomes "Senior Software Engineer, Full Stack: View & Apply".
                links = page.evaluate("""() => Array.from(document.querySelectorAll('a[href]')).map(a => {
                    const own = (a.innerText || '').trim();
                    const row = a.closest('li, tr, article, [class*=job], [class*=posting], [class*=opening], [class*=card], [class*=role]') || a.parentElement;
                    const ctx = row ? (row.innerText || '').trim().split('\\n').map(l => l.trim()).filter(Boolean)[0] || '' : '';
                    const label = own && ctx && !own.startsWith(ctx) && !ctx.startsWith(own) ? ctx + ': ' + own : (ctx || own);
                    return [label, a.href];
                })""")
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - a page that will not render is a source that yields nothing
        log.warning("could not render %s (%s); falling back to plain fetch", url, exc)
        return _plain(url)

    links = [(clean(t), h) for t, h in links if h and h.startswith("http")]
    return Rendered(url=url, text=clean(text)[:MAX_TEXT_CHARS], links=links[:MAX_LINKS], requests=requests, rendered=True)
