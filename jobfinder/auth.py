"""Who is asking.

Accounts are created by the admin (CLI or the Users section); there is no
self-registration and no second factor. Passwords are never stored: only an
Argon2id hash (pwdlib), which cannot be reversed if the database is stolen.

A login returns an opaque bearer token: 32 random bytes, shown once. The
database keeps only its SHA-256, so a stolen database yields no usable
token either. Every request carries ``Authorization: Bearer <token>`` —
never a token in the URL, which would leak into access logs, browser history
and the Referer of every Apply link.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request
from pwdlib import PasswordHash

from . import db

TOKEN_DAYS = 30
_hasher = PasswordHash.recommended()   # Argon2id


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _hasher.verify(password, password_hash)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def login(email: str, password: str, name: str = "") -> dict[str, Any] | None:
    """A fresh token for these credentials, or None. Constant-ish time: a
    dummy verification runs when the email is unknown so the response does
    not reveal which emails exist."""
    found = db.get_password_hash(email)
    if not found:
        _hasher.verify(password, _DUMMY_HASH)
        return None
    user_id, stored = found
    if not verify_password(password, stored):
        return None
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=TOKEN_DAYS)).isoformat(timespec="seconds")
    db.create_token(user_id, _token_hash(token), name=name, expires_at=expires)
    return {"token": token, "expires_at": expires, "user": db.get_user(user_id)}


_DUMMY_HASH = hash_password("not-a-real-password")


def check_password(user_id: int, password: str) -> bool:
    found = db.get_password_hash(user_id=user_id)
    return bool(found) and verify_password(password, found[1])


def user_for_token(token: str) -> dict[str, Any] | None:
    return db.resolve_token(_token_hash(token))


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def current_user(request: Request) -> dict[str, Any]:
    """FastAPI dependency: the user behind the bearer token, or 401."""
    token = _bearer(request)
    user = user_for_token(token) if token else None
    if not user:
        raise HTTPException(401, "not logged in", headers={"WWW-Authenticate": "Bearer"})
    return user


def admin_user(user: Annotated[dict[str, Any], Depends(current_user)]) -> dict[str, Any]:
    if not user["is_admin"]:
        raise HTTPException(403, "admin only")
    return user


CurrentUser = Annotated[dict[str, Any], Depends(current_user)]
AdminUser = Annotated[dict[str, Any], Depends(admin_user)]
