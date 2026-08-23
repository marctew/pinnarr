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

#: Registered so they can be triggered — by hand, or scheduled once in
#: response to something you did — but deliberately never on a cron. Named
#: here rather than assumed, so the check that every registered job is
#: scheduled can still tell a deliberate omission from a drifted one.
ON_DEMAND: frozenset[str] = frozenset({"find_requested"})


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
        cast_sync,
        guides,
        housekeeping,
        notifications,
        overseerr_sync,
        plex_sync,
        queue_sync,
        sonarr_sync,
        suggest,
        tag_sync,
        tautulli_sync,
        tmdb_sync,
        watch_state,
        watchlist_sync,
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
    nightly(2, 45, watch_state.sync_all_watch_state, "plex_watched_full")
    nightly(3, 15, guides.refresh_pinned_guides, "pinned_guides")
    nightly(3, 35, suggest.refresh_suggestions, "suggestions")
    nightly(3, 45, cast_sync.sync_cast, "tmdb_cast")
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
        watchlist_sync.sync_watchlist,
        CronTrigger(minute="*/10"),
        id="plex_watchlist", replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        tag_sync.sync_tags,
        # Often enough that tagging in Sonarr feels like it did something.
        CronTrigger(minute="*/10"),
        id="sonarr_tags", replace_existing=True, max_instances=1, coalesce=True,
    )
    # Often enough that a request made here stops saying "pending" soon
    # after it stops being pending there.
    scheduler.add_job(
        overseerr_sync.sync_requests,
        CronTrigger(minute="*/15"),
        id="overseerr_requests", replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        queue_sync.sync_queue,
        # Every minute: a progress bar that updates every five is a
        # screenshot, not progress. One LAN call a minute is cheap.
        CronTrigger(minute="*"),
        id="sonarr_queue", replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        notifications.notify_pending,
        CronTrigger(minute="*"),
        id="notify_pending", replace_existing=True, max_instances=1, coalesce=True,
    )
    # Hourly: what Plex says you have watched, including anything toggled
    # rather than played. Tautulli's full history sweep stays nightly.
    scheduler.add_job(
        tautulli_sync.sync_recent_history,
        CronTrigger(minute=50),
        id="tautulli_recent", replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        watch_state.sync_watch_state,
        CronTrigger(minute=40),
        id="plex_watched", replace_existing=True, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        availability.sync_availability,
        CronTrigger(minute=25),
        id="plex_availability", replace_existing=True, max_instances=1, coalesce=True,
    )

    # Alongside the digest rather than daily: a suggestion is not urgent, and
    # a daily one is an irritation.
    scheduler.add_job(
        notifications.notify_new_seasons,
        CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=settings.tz),
        id="season_alerts", replace_existing=True, max_instances=1, coalesce=True,
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
