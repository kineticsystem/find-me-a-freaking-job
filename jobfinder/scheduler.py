"""Interval scheduling around pipeline.run.run_once."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings
from .pipeline.run import run_once

log = logging.getLogger(__name__)

JOB_ID = "jobfinder-pipeline"
_scheduler: BackgroundScheduler | None = None


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    cfg = settings()
    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(
        run_once,
        trigger=IntervalTrigger(minutes=cfg.interval_minutes),
        id=JOB_ID,
        name="job search pipeline",
        # A run can outlast the interval on a local 27B; never stack them, and
        # collapse anything missed while one was in flight.
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    sched.start()
    _scheduler = sched
    log.info("scheduler started: every %d minutes (interval_minutes in config/settings.yaml)", cfg.interval_minutes)

    if cfg.run_on_start:
        sched.add_job(run_once, id=f"{JOB_ID}-initial", name="initial run")
        log.info("initial run queued")
    return sched


def stop() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None


def next_run() -> str | None:
    if not _scheduler:
        return None
    job = _scheduler.get_job(JOB_ID)
    return job.next_run_time.isoformat() if job and job.next_run_time else None


def set_interval(minutes: int) -> bool:
    """Reschedule the periodic run. The next run is `minutes` from now."""
    if not _scheduler or not _scheduler.running:
        return False
    _scheduler.reschedule_job(JOB_ID, trigger=IntervalTrigger(minutes=minutes))
    log.info("interval changed: every %d minutes", minutes)
    return True


def trigger_now() -> bool:
    """Queue an immediate run on the scheduler's own executor."""
    if not _scheduler or not _scheduler.running:
        return False
    _scheduler.add_job(run_once, name="manual run", misfire_grace_time=None)
    return True
