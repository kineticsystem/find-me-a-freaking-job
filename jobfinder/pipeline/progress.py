"""Where the current scan is, for the UI.

A single process-wide status, updated by the pipeline stages and read by
/health. Stages that loop over items (triage batches, deep dives, extraction
batches) report each unit's duration, and the estimate for the rest of the
stage is the average of the units done so far -- honest to within a few
minutes, since one model call can take anywhere from forty seconds to four.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_state: dict[str, Any] = {"active": False}
_stop = threading.Event()


def request_stop() -> bool:
    """Ask the running scan to stop after its current unit. Returns False if
    no scan is running. The in-flight model call is killed too, so the wait
    is seconds, not minutes."""
    with _lock:
        if not _state.get("active"):
            return False
        _state["stopping"] = True
    _stop.set()
    from .. import opencode
    opencode.kill_current()
    return True


def stop_requested() -> bool:
    return _stop.is_set()


def begin(run_id: int) -> None:
    from .. import opencode
    _stop.clear()
    opencode.reset_cancel()
    with _lock:
        _state.clear()
        _state.update({
            "active": True, "run_id": run_id, "started_at": time.time(),
            "stage": "starting", "message": "starting", "current": 0, "total": 0,
            "stage_started_at": time.time(), "unit_seconds": [], "fetched": 0, "sources": 0,
        })


def stage(name: str, message: str, total: int = 0) -> None:
    """Enter a stage. `total` is the number of units it will report on."""
    with _lock:
        if not _state.get("active"):
            return
        _state.update({"stage": name, "message": message, "current": 0, "total": total,
                       "stage_started_at": time.time(), "unit_seconds": []})


def unit_done(seconds: float, message: str | None = None) -> None:
    """One unit of the current stage finished (a batch, a job)."""
    with _lock:
        if not _state.get("active"):
            return
        _state["current"] += 1
        _state["unit_seconds"].append(seconds)
        if message:
            _state["message"] = message


def note(**fields: Any) -> None:
    with _lock:
        if _state.get("active"):
            _state.update(fields)


def finish() -> None:
    _stop.clear()
    with _lock:
        _state.clear()
        _state["active"] = False


def snapshot() -> dict[str, Any]:
    """What /health reports. ETA only for the current stage."""
    with _lock:
        if not _state.get("active"):
            return {"active": False}
        s = dict(_state)
    done, total, units = s["current"], s["total"], s["unit_seconds"]
    eta = None
    if total and units:
        eta = int((total - done) * (sum(units) / len(units)))
    return {
        "active": True,
        "run_id": s["run_id"],
        "stage": s["stage"],
        "message": s["message"],
        "current": done,
        "total": total,
        "eta_seconds": eta,
        "elapsed_seconds": int(time.time() - s["started_at"]),
        "fetched": s.get("fetched", 0),
        "sources": s.get("sources", 0),
        "stopping": bool(s.get("stopping")),
    }
