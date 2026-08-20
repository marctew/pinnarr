"""Scheduled jobs and the scheduler that runs them.

Every job is wrapped by @tracked, which records start/finish in sync_log and
swallows exceptions. A job that throws must not kill the scheduler or take
the web UI down with it — the failure shows up on /healthz instead.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.db import job_finished, job_started

log = logging.getLogger(__name__)

#: name → coroutine, for the manual /api/sync/{job} trigger.
REGISTRY: dict[str, Callable[[], Awaitable[Any]]] = {}


def tracked(name: str) -> Callable:
    def decorator(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            run_id = job_started(name)
            try:
                detail = await fn(*args, **kwargs) or ""
                job_finished(run_id, "ok", detail)
                log.info("job %s ok: %s", name, detail)
                return detail
            except Exception as exc:  # noqa: BLE001 — deliberate catch-all
                log.exception("job %s failed", name)
                job_finished(run_id, "error", f"{type(exc).__name__}: {exc}")
                return f"error: {exc}"

        REGISTRY[name] = wrapper
        return wrapper

    return decorator


def build_scheduler() -> AsyncIOScheduler:
    """Wire the cron schedule. Times are SPEC §8."""
    # Imported here so the @tracked decorators have run and populated REGISTRY.
    from app.jobs import (
        availability,
        housekeeping,
        notifications,
        plex_sync,
        sonarr_sync,
        tautulli_sync,
        tmdb_sync,
    )

    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.tz)

    def nightly(hour: int, minute: int, fn: Callable, job_id: str) -> None:
        scheduler.add_job(
            fn, CronTrigger(hour=hour, minute=minute), id=job_id, replace_existing=True,
            max_instances=1, coalesce=True,
        )

    nightly(3, 0, plex_sync.sync_plex_library, "plex_library")
    nightly(3, 10, sonarr_sync.sync_sonarr_series, "sonarr_series")
    nightly(3, 20, tautulli_sync.sync_tautulli_history, "tautulli_history")
    nightly(3, 30, tmdb_sync.sync_outlook, "tmdb_status")
    nightly(3, 40, notifications.notify_schedule_changes, "schedule_changes")
    nightly(4, 0, notifications.reconcile, "reconcile")
    nightly(4, 30, housekeeping.housekeeping, "housekeeping")

    scheduler.add_job(
        sonarr_sync.sync_sonarr_calendar,
        CronTrigger(hour="*/2", minute=5),
        id="sonarr_calendar", replace_existing=True, max_instances=1, coalesce=True,
    )
    # Often, and usually a no-op: it only acts once a batch has settled.
    scheduler.add_job(
        notifications.notify_pending,
        CronTrigger(minute="*"),
        id="notify_pending", replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        availability.sync_availability,
        CronTrigger(minute=25),
        id="plex_availability", replace_existing=True, max_instances=1, coalesce=True,
    )

    if settings.digest_enabled:
        try:
            scheduler.add_job(
                notifications.weekly_digest,
                CronTrigger.from_crontab(settings.digest_cron, timezone=settings.tz),
                id="weekly_digest", replace_existing=True, max_instances=1, coalesce=True,
            )
        except ValueError:
            log.error("DIGEST_CRON %r is not a valid cron expression, digest disabled",
                      settings.digest_cron)

    return scheduler
