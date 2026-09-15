"""Who the search is for: each user's CV, notes and preferences, and the
model's digest of the CV.

Everything lives in the database (`user_profile`, `user_preferences`), one
row per user, so a backup of jobs.db is everything.

The digest goes into every reasoning prompt, so it must stay under roughly
800 tokens. It is re-derived only when the CV or the notes actually change.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import db
from ..config import DEFAULT_USER_ID, Preferences, preferences
from ..models import ProfileDigest
from ..opencode import run_session
from ..prompts import profile_prompt
from ..textutil import clean, truncate

log = logging.getLogger(__name__)

CV_SUFFIXES = (".md", ".txt", ".pdf")
MAX_CV_CHARS = 24000
CV_MAX_BYTES = 10 * 1024 * 1024
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


@dataclass
class Candidate:
    """One user as the pipeline sees them. Built by `load`; the digest is
    filled in by `load_digest` when a model is available."""

    user_id: int
    name: str
    prefs: Preferences
    cv_text: str
    notes: str
    digest: ProfileDigest | None = None
    stale_digest: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def source_hash(self) -> str:
        """Identity of the CV + notes the digest is derived from."""
        return hashlib.sha256((self.cv_text + "\x00" + self.notes).encode()).hexdigest()[:16]

    @property
    def criteria(self) -> str:
        from .criteria import criteria_hash

        return criteria_hash(self.source_hash, self.prefs.fingerprint)

    @property
    def readiness(self) -> dict[str, bool]:
        cv_ok = bool(self.cv_text.strip())
        notes_ok = bool(self.notes.strip())
        prefs_ok = bool(self.prefs.titles) and bool(self.prefs.based_in.strip())
        return {"cv": cv_ok, "notes": notes_ok, "preferences": prefs_ok, "ready": cv_ok and notes_ok and prefs_ok}

    @property
    def ready(self) -> bool:
        return self.readiness["ready"]


def _read_pdf(data: bytes, name: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        log.warning("pypdf not installed; cannot read %s", name)
        return ""
    try:
        return "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(data)).pages)
    except Exception as exc:
        log.warning("could not read %s: %s", name, exc)
        return ""


def extract_cv_text(name: str, data: bytes) -> str:
    text = _read_pdf(data, name) if name.lower().endswith(".pdf") else data.decode("utf-8", errors="replace")
    text = clean(text)
    return truncate(f"--- {name} ---\n{text}", MAX_CV_CHARS) if text else ""


def _clean_notes(raw: str) -> str:
    """HTML comments removed: the example file is one long comment, so an
    untouched template reads as empty."""
    return clean(_HTML_COMMENT.sub("", raw or ""))


def load(user_id: int) -> Candidate:
    row = db.get_profile(user_id)
    user = db.get_user(user_id)
    cand = Candidate(
        user_id=user_id,
        name=(user or {}).get("email") or (user or {}).get("name") or f"user {user_id}",
        prefs=preferences(user_id),
        cv_text=row["cv_text"] or "",
        notes=_clean_notes(row["notes"]),
    )
    if row["digest"] and row["digest_hash"] == cand.source_hash:
        try:
            cand.digest = ProfileDigest.model_validate(json.loads(row["digest"]))
        except Exception as exc:
            log.warning("stored digest for user %d unreadable (%s); will rebuild", user_id, exc)
    return cand


def candidates() -> list[Candidate]:
    """Every user who can be searched for: CV, notes and preferences all present."""
    return [c for c in (load(u["id"]) for u in db.list_users()) if c.ready]


def cv_text(user_id: int = DEFAULT_USER_ID) -> str:
    return db.get_profile(user_id)["cv_text"] or ""


def notes_text(user_id: int = DEFAULT_USER_ID) -> str:
    return _clean_notes(db.get_profile(user_id)["notes"])


def readiness(user_id: int = DEFAULT_USER_ID) -> dict[str, bool]:
    return load(user_id).readiness


def load_digest(cand: Candidate, workdir: Path, force: bool = False) -> ProfileDigest:
    """The stored digest if it matches the current CV + notes, else a fresh
    one from the model, stored."""
    if cand.digest is not None and not force:
        return cand.digest
    if not cand.cv_text.strip():
        raise FileNotFoundError(f"No CV for {cand.name}: upload one in the web app (Settings → Profile).")
    log.info("building profile digest for %s", cand.name)
    digest = run_session(
        profile_prompt(cand.cv_text, cand.notes),
        workdir / f"profile-{cand.user_id}",
        ProfileDigest,
        title=f"profile digest ({cand.name})",
    )
    db.save_digest(cand.user_id, cand.source_hash, digest.model_dump_json(indent=2))
    cand.digest = digest
    log.info("profile digest built for %s (%s)", cand.name, digest.headline or "unnamed")
    return digest


# --------------------------------------------------------------------------
# Editing the profile from the API
# --------------------------------------------------------------------------
def replace_cv(user_id: int, filename: str, data: bytes) -> str:
    """Store an uploaded CV as this user's; the digest is keyed on the CV's
    content, so the next run rebuilds it and, through the criteria hash,
    re-scores every job for them."""
    ext = Path(filename).suffix.lower()
    if ext not in CV_SUFFIXES:
        raise ValueError(f"unsupported file type {ext or '(none)'}; use one of {', '.join(CV_SUFFIXES)}")
    if not data:
        raise ValueError("the file is empty")
    if len(data) > CV_MAX_BYTES:
        raise ValueError(f"file is larger than {CV_MAX_BYTES // (1024 * 1024)} MB")
    if ext == ".pdf" and not data.startswith(b"%PDF"):
        raise ValueError("that does not look like a PDF")
    name = f"cv{ext}"
    db.save_cv(user_id, name, data, extract_cv_text(name, data))
    log.info("CV replaced for user %d: %s (%d bytes)", user_id, name, len(data))
    return name


def write_notes(user_id: int, text: str) -> None:
    db.save_notes(user_id, text.rstrip() + "\n" if text.strip() else "")


def profile_status(user_id: int) -> dict[str, Any]:
    """What the UI shows: the CV, the notes, the digest if it is current."""
    row = db.get_profile(user_id)
    cand = load(user_id)
    cv = {"name": row["cv_name"], "bytes": row["cv_bytes"], "modified": row["cv_updated_at"]} if row["cv_name"] else None
    return {
        "cv": cv,
        "cv_files": [row["cv_name"]] if row["cv_name"] else [],
        "cv_chars": len(cand.cv_text),
        "notes": row["notes"] or "",
        "digest": cand.digest.model_dump() if cand.digest else None,
        "digest_current": cand.digest is not None,
    }
