"""Turn the CV folder into a small structured digest, once, and cache it.

The digest goes into every reasoning prompt, so it must stay under roughly 800
tokens. It is re-derived only when the CV or the notes actually change.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..models import ProfileDigest
from ..opencode import run_session
from ..prompts import profile_prompt
from ..textutil import clean, truncate

log = logging.getLogger(__name__)

CV_SUFFIXES = (".md", ".txt", ".pdf")
MAX_CV_CHARS = 24000


def profile_dir() -> Path:
    p = settings().paths.resolve("profile")
    (p / ".cache").mkdir(parents=True, exist_ok=True)
    return p


def _cv_files() -> list[Path]:
    """Only files named cv.<ext> (cv.pdf, cv.md, cv.txt) count as the CV. The
    folder also holds notes, an example and a README, none of which are."""
    root = profile_dir()
    return sorted(
        p for p in root.iterdir()
        if p.is_file() and p.suffix.lower() in CV_SUFFIXES and p.stem.lower() == "cv"
    )


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        log.warning("pypdf not installed; cannot read %s", path.name)
        return ""
    try:
        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    except Exception as exc:
        log.warning("could not read %s: %s", path.name, exc)
        return ""


def cv_text() -> str:
    chunks = []
    for path in _cv_files():
        text = _read_pdf(path) if path.suffix.lower() == ".pdf" else path.read_text(errors="replace")
        text = clean(text)
        if text:
            chunks.append(f"--- {path.name} ---\n{text}")
    return truncate("\n\n".join(chunks), MAX_CV_CHARS)


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


def notes_text() -> str:
    """The notes with HTML comments removed: the example file is one long
    comment, so an untouched file reads as empty."""
    notes = profile_dir() / "notes.md"
    if not notes.exists():
        return ""
    return clean(_HTML_COMMENT.sub("", notes.read_text(errors="replace")))


def source_hash() -> str:
    return hashlib.sha256((cv_text() + "\x00" + notes_text()).encode()).hexdigest()[:16]


def _cache_path() -> Path:
    return profile_dir() / ".cache" / "profile.json"


def profile_fingerprint() -> str:
    """Identity of the digest currently in force (it is derived from the CV)."""
    return source_hash()


def load_digest(workdir: Path, force: bool = False) -> ProfileDigest:
    """Return the cached digest, rebuilding it if the CV changed."""
    cache = _cache_path()
    current = source_hash()

    if not force and cache.exists():
        try:
            data = json.loads(cache.read_text())
            if data.get("source_hash") == current:
                return ProfileDigest.model_validate(data["digest"])
            log.info("CV or notes changed; rebuilding profile digest")
        except Exception as exc:
            log.warning("profile cache unreadable (%s); rebuilding", exc)

    text = cv_text()
    if not text.strip():
        raise FileNotFoundError(
            f"No CV found. Put a cv.pdf, cv.md or cv.txt in {profile_dir()} "
            "and describe what you want in notes.md."
        )

    digest = run_session(
        profile_prompt(text, notes_text()),
        workdir / "profile",
        ProfileDigest,
        title="profile digest",
    )
    cache.write_text(
        json.dumps({"source_hash": current, "digest": digest.model_dump()}, indent=2)
    )
    log.info("profile digest rebuilt (%s)", digest.headline or "unnamed")
    return digest


# --------------------------------------------------------------------------
# Editing the profile from the API
# --------------------------------------------------------------------------
CV_MAX_BYTES = 10 * 1024 * 1024


def cv_files() -> list[Path]:
    return _cv_files()


def replace_cv(filename: str, data: bytes) -> Path:
    """Store an uploaded CV as profile/cv.<ext>, removing any previous CV.

    The digest cache is keyed on the CV's content, so the next run rebuilds
    the profile and, through the criteria hash, re-scores every job.
    """
    ext = Path(filename).suffix.lower()
    if ext not in CV_SUFFIXES:
        raise ValueError(f"unsupported file type {ext or '(none)'}; use one of {', '.join(CV_SUFFIXES)}")
    if not data:
        raise ValueError("the file is empty")
    if len(data) > CV_MAX_BYTES:
        raise ValueError(f"file is larger than {CV_MAX_BYTES // (1024 * 1024)} MB")
    if ext == ".pdf" and not data.startswith(b"%PDF"):
        raise ValueError("that does not look like a PDF")

    root = profile_dir()
    for old in _cv_files():
        old.unlink()
    target = root / f"cv{ext}"
    target.write_bytes(data)
    _cache_path().unlink(missing_ok=True)
    log.info("CV replaced: %s (%d bytes)", target.name, len(data))
    return target


def write_notes(text: str) -> Path:
    path = profile_dir() / "notes.md"
    path.write_text(text.rstrip() + "\n")
    _cache_path().unlink(missing_ok=True)
    return path


def readiness() -> dict:
    """What the first scan still needs. The UI shows this as a checklist and
    the pipeline will not score until `ready` is true."""
    from ..config import preferences

    prefs = preferences()
    cv_ok = len(cv_text()) > 0
    notes_ok = len(notes_text()) > 0
    prefs_ok = bool(prefs.titles) and bool(prefs.based_in.strip())
    return {
        "cv": cv_ok,
        "notes": notes_ok,
        "preferences": prefs_ok,
        "ready": cv_ok and notes_ok and prefs_ok,
    }


def profile_status() -> dict:
    """What the UI shows: the CV on disk, the notes, and the cached digest."""
    files = _cv_files()
    cv = None
    if files:
        f = files[0]
        st = f.stat()
        cv = {"name": f.name, "bytes": st.st_size, "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")}
    digest = None
    cache = _cache_path()
    if cache.exists():
        try:
            data = json.loads(cache.read_text())
            if data.get("source_hash") == source_hash():
                digest = data["digest"]
        except Exception:
            digest = None
    return {
        "cv": cv,
        "cv_files": [f.name for f in files],
        "cv_chars": len(cv_text()),
        "notes": notes_text(),
        "digest": digest,
        "digest_current": digest is not None,
    }
