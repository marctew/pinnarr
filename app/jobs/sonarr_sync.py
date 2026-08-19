"""Sonarr sync: series metadata nightly, calendar every two hours."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from app.clients.sonarr import SonarrClient
from app.config import get_settings
from app.db import session
from app.jobs import tracked
from app.repo import resolve_series_id, upsert_episode, upsert_from_sonarr

log = logging.getLogger(__name__)

#: How far back and forward the calendar window reaches.
CALENDAR_PAST_DAYS = 7
CALENDAR_FUTURE_DAYS = 60


@tracked("sonarr_series")
async def sync_sonarr_series() -> str:
    settings = get_settings()
    if not settings.sonarr_configured:
        return "skipped: Sonarr not configured"

    series = await SonarrClient().series()
    with session() as conn:
        for s in series:
            upsert_from_sonarr(conn, s)

    return f"{len(series)} series"


@tracked("sonarr_calendar")
async def sync_sonarr_calendar() -> str:
    settings = get_settings()
    if not settings.sonarr_configured:
        return "skipped: Sonarr not configured"

    today = datetime.now(UTC).date()
    start = today - timedelta(days=CALENDAR_PAST_DAYS)
    end = today + timedelta(days=CALENDAR_FUTURE_DAYS)

    episodes = await SonarrClient().calendar(start, end)

    stored = 0
    orphaned = 0
    with session() as conn:
        # Cache the sonarr_id → series_id mapping; a calendar window can hold
        # many episodes of the same series.
        cache: dict[int, int | None] = {}
        for ep in episodes:
            key = ep.sonarr_series_id
            if key not in cache:
                series_id, _ = resolve_series_id(
                    conn, tvdb_id=ep.tvdb_id, sonarr_id=ep.sonarr_series_id
                )
                cache[key] = series_id
            series_id = cache[key]

            if series_id is None:
                # Sonarr knows a series we've never seen. It'll appear after the
                # next sonarr_series run; skip rather than inventing a row here.
                orphaned += 1
                continue

            upsert_episode(conn, series_id, ep)
            stored += 1

    detail = f"{stored} episodes ({start} → {end})"
    if orphaned:
        detail += f"; {orphaned} awaiting a series sync"
    return detail
