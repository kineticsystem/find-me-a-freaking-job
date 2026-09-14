"""HTML/text hygiene. Raw HTML must never reach the model."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_SKIP = {"script", "style", "noscript", "svg", "head"}
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "ul", "ol"}


class _Stripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _SKIP:
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag in _BLOCK and tag != "li":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    """HTML (or entity-escaped HTML, as Greenhouse and Arbeitnow send it) to
    readable text: block elements become line breaks, list items get a bullet.
    """
    if not raw:
        return ""
    # Some boards escape the markup: "&lt;p&gt;". Unescape until stable, then
    # decide whether there is markup to strip.
    text = raw
    for _ in range(3):
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    if "<" not in text:
        return clean(text)
    parser = _Stripper()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        return clean(re.sub(r"<[^>]+>", " ", text))
    return clean("".join(parser.parts))


def clean(text: str) -> str:
    text = html.unescape(text or "")
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def truncate(text: str, max_chars: int, note: str = "\n\n[... truncated ...]") -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Prefer a paragraph boundary so we don't slice a sentence in half.
    boundary = cut.rfind("\n\n")
    if boundary > max_chars * 0.6:
        cut = cut[:boundary]
    return cut + note


_REMOTE_RX = re.compile(r"\b(fully[- ]remote|100% remote|remote[- ]first|work from anywhere|remote)\b", re.I)
_HYBRID_RX = re.compile(r"\bhybrid\b", re.I)
_ONSITE_RX = re.compile(r"\b(on[- ]site|onsite|in[- ]office|in person)\b", re.I)


def infer_remote(*fields: str) -> str:
    blob = " ".join(f for f in fields if f)[:4000]
    if _HYBRID_RX.search(blob):
        return "hybrid"
    if _REMOTE_RX.search(blob):
        return "remote"
    if _ONSITE_RX.search(blob):
        return "onsite"
    return "unknown"


_SALARY_RX = re.compile(
    r"(?:[$€£]|USD|EUR|GBP|PLN)\s?\d[\d,.]*\s?(?:k\b)?(?:\s?(?:-|–|to)\s?(?:[$€£]|USD|EUR|GBP|PLN)?\s?\d[\d,.]*\s?(?:k\b)?)?",
    re.I,
)


def find_salary(*fields: str) -> str:
    for field in fields:
        if not field:
            continue
        m = _SALARY_RX.search(field)
        if m:
            return m.group(0).strip()
    return ""
