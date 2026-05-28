"""APScheduler background jobs: prune watched, sync new episodes."""

from __future__ import annotations

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

import service
from service import refresh_franchise_definitions as _refresh_franchise_definitions

log = logging.getLogger(__name__)


def _interval_minutes() -> int:
    try:
        return max(1, int(os.environ.get("PRUNE_INTERVAL_MINUTES", "10")))
    except ValueError:
        return 10


def _auto_sync_enabled() -> bool:
    val = os.environ.get("AUTO_SYNC", "true").strip().lower()
    return val not in ("false", "0", "no", "off")


def _refresh_genre_cache() -> None:
    import db
    from media_client import available_backends, get_client
    for backend in available_backends():
        try:
            genres = get_client(backend).list_all_genres()
            db.set_genre_cache(backend, genres)
            log.info(
                "Genre cache refreshed: %d genres on %s", len(genres), backend
            )
        except Exception:
            log.exception("Genre cache refresh failed for %s", backend)


def _refresh_franchise_definitions_job() -> None:
    try:
        _refresh_franchise_definitions()
        log.info("Franchise definitions refreshed")
    except Exception:
        log.warning("Franchise definition refresh failed", exc_info=True)


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
    sync_on = _auto_sync_enabled()
    if sync_on:
        sched.add_job(
            service.sync_all,
            "interval",
            minutes=minutes,
            id="sync_all",
            max_instances=1,
            coalesce=True,
        )
    # Genre cache: fire once at startup, then weekly.
    sched.add_job(
        _refresh_genre_cache,
        "date",
        id="genre_cache_init",
        misfire_grace_time=600,
    )
    sched.add_job(
        _refresh_genre_cache,
        "interval",
        days=7,
        id="genre_cache_weekly",
        max_instances=1,
        coalesce=True,
    )
    # v2.2.0 — weekly franchise definition refresh
    sched.add_job(
        _refresh_franchise_definitions_job,
        "interval",
        weeks=1,
        id="refresh_franchise_definitions",
        replace_existing=True,
    )
    sched.start()
    log.info(
        "Background jobs scheduled every %d minute(s): prune%s",
        minutes,
        " + sync" if sync_on else " (auto-sync disabled)",
    )
    return sched
