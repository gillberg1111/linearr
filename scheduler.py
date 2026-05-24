"""APScheduler background job that prunes managed playlists periodically."""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

import service

log = logging.getLogger(__name__)


def _interval_minutes() -> int:
    try:
        return max(1, int(os.environ.get("PRUNE_INTERVAL_MINUTES", "10")))
    except ValueError:
        return 10


def start() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="UTC")
    minutes = _interval_minutes()
    sched.add_job(
        service.prune_all,
        "interval",
        minutes=minutes,
        id="prune_all",
        max_instances=1,
        coalesce=True,
    )
    sched.start()
    log.info("Background prune scheduled every %d minute(s)", minutes)
    return sched
