"""Arrival notifications, the reconcile safety net, and the weekly digest."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from app.clients import ntfy
from app.config import get_settings
from app.db import session, utcnow
from app.jobs import tracked
from app.outlook import parse_dt

log = logging.getLogger(__name__)

#: Don't fire a late notification for something that arrived ages ago — if the
#: webhook missed it a week back, a push now is noise, not news.
RECONCILE_WINDOW_HOURS = 48


def _episode_code(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


async def notify_arrival(series_id: int, season: int, episode: int) -> bool:
    """Push "it's here" for one episode, if the series is pinned and un-notified.

    Idempotent on notified_at, so Sonarr's On Upgrade event (720p → 1080p)
    doesn't produce a second buzz for the same episode.
    """
    settings = get_settings()
    if not settings.notify_on_arrival:
        return False

    with session() as conn:
        row = conn.execute(
            """
            SELECT e.id AS episode_id, e.title AS episode_title, e.notified_at,
                   s.title AS series_title, s.pinned, s.notify
            FROM episodes e JOIN series s ON s.id = e.series_id
            WHERE e.series_id = ? AND e.season = ? AND e.episode = ?
            """,
            (series_id, season, episode),
        ).fetchone()

    if not row:
        log.debug("no episode row for series=%s %s", series_id, _episode_code(season, episode))
        return False
    if not row["pinned"] or not row["notify"]:
        return False
    if row["notified_at"]:
        return False

    code = _episode_code(season, episode)
    title = f"{row['series_title']} {code} is in Plex"
    body = row["episode_title"] or "Just arrived and ready to watch."

    sent = await ntfy.send(title, body, tags="tv,white_check_mark")
    if sent:
        with session() as conn:
            conn.execute(
                "UPDATE episodes SET notified_at = ?, updated_at = ? WHERE id = ?",
                (utcnow(), utcnow(), row["episode_id"]),
            )
    return sent


@tracked("reconcile")
async def reconcile() -> str:
    """Catch arrivals the webhook missed.

    Sonarr's webhook is the primary path — instant and free. This is the
    belt-and-braces pass for a dropped delivery or a restart at the wrong moment.
    """
    cutoff = (datetime.now(UTC) - timedelta(hours=RECONCILE_WINDOW_HOURS)).isoformat()

    with session() as conn:
        pending = conn.execute(
            """
            SELECT e.series_id, e.season, e.episode
            FROM episodes e JOIN series s ON s.id = e.series_id
            WHERE s.pinned = 1 AND s.notify = 1
              AND e.notified_at IS NULL
              AND (e.has_file = 1 OR e.in_plex = 1)
              AND e.arrived_at IS NOT NULL AND e.arrived_at >= ?
            ORDER BY e.arrived_at
            """,
            (cutoff,),
        ).fetchall()

    sent = 0
    for row in pending:
        if await notify_arrival(row["series_id"], row["season"], row["episode"]):
            sent += 1

    # Mark anything older than the window as notified so it never fires late.
    with session() as conn:
        stale = conn.execute(
            """
            UPDATE episodes SET notified_at = ?
            WHERE notified_at IS NULL AND (has_file = 1 OR in_plex = 1)
              AND (arrived_at IS NULL OR arrived_at < ?)
            """,
            (utcnow(), cutoff),
        ).rowcount

    return f"{sent} late notification(s) sent, {stale} older arrivals suppressed"


@tracked("weekly_digest")
async def weekly_digest() -> str:
    """One push listing the coming week's pinned episodes, grouped by day."""
    settings = get_settings()
    if not settings.ntfy_configured:
        return "skipped: ntfy not configured"

    now = datetime.now(UTC)
    end = now + timedelta(days=7)

    with session() as conn:
        rows = conn.execute(
            """
            SELECT s.title AS series_title, e.season, e.episode, e.air_date_utc
            FROM episodes e JOIN series s ON s.id = e.series_id
            WHERE s.pinned = 1 AND e.air_date_utc IS NOT NULL
              AND e.air_date_utc >= ? AND e.air_date_utc < ?
              AND e.season > 0
            ORDER BY e.air_date_utc
            """,
            (now.isoformat(), end.isoformat()),
        ).fetchall()

    if not rows:
        # Silence beats "nothing this week" landing on your phone every Monday.
        return "nothing scheduled, digest suppressed"

    from zoneinfo import ZoneInfo

    local = ZoneInfo(settings.tz)
    by_day: dict[str, list[str]] = {}
    for row in rows:
        dt = parse_dt(row["air_date_utc"])
        if not dt:
            continue
        day = dt.astimezone(local).strftime("%a %-d %b")
        by_day.setdefault(day, []).append(
            f"{row['series_title']} {_episode_code(row['season'], row['episode'])}"
        )

    lines = [f"{day}\n  " + "\n  ".join(items) for day, items in by_day.items()]
    body = "\n".join(lines)
    count = len(rows)

    await ntfy.send(
        f"{count} episode{'s' if count != 1 else ''} this week",
        body,
        tags="calendar",
        priority="low",
    )
    return f"digest sent: {count} episodes across {len(by_day)} day(s)"
