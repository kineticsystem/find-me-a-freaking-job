from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Type

import httpx

from ..config import settings
from ..models import RawJob

log = logging.getLogger(__name__)

REGISTRY: dict[str, Type["Source"]] = {}


def register(name: str) -> Callable[[Type["Source"]], Type["Source"]]:
    def deco(cls: Type["Source"]) -> Type["Source"]:
        cls.type_name = name
        REGISTRY[name] = cls
        return cls

    return deco


_last_call: dict[str, float] = {}
_lock = threading.Lock()


def _be_polite(host: str) -> None:
    """One request per host per politeness_delay, across threads."""
    delay = settings().http.politeness_delay_seconds
    if delay <= 0:
        return
    with _lock:
        prev = _last_call.get(host, 0.0)
        wait = delay - (time.time() - prev)
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.time()


def http_client() -> httpx.Client:
    cfg = settings().http
    return httpx.Client(
        timeout=cfg.timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": cfg.user_agent, "Accept": "*/*"},
    )


def fetch_url(url: str, *, client: httpx.Client | None = None, **kwargs: Any) -> httpx.Response:
    owned = client is None
    client = client or http_client()
    try:
        _be_polite(httpx.URL(url).host or url)
        resp = client.get(url, **kwargs)
        resp.raise_for_status()
        return resp
    finally:
        if owned:
            client.close()


class Source:
    """A place jobs come from."""

    type_name = "base"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        self.id: str = cfg["id"]

    def fetch(self) -> list[RawJob]:  # pragma: no cover - interface
        raise NotImplementedError

    # convenience
    def _job(self, **kw: Any) -> RawJob:
        return RawJob(source_id=self.id, **kw)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id}>"
