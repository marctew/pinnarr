"""Arrival notifications, the reconcile safety net, and the weekly digest."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from app.clients import ntfy
from app.config import get_settings
from app.db import session, utcnow
from app.jobs import tracked

log = logging.getLogger(__name__)

#: Don't fire a late notification for something that arrived ages ago — if the
#: webhook missed it a week back, a push now is noise, not news.
RECONCILE_WINDOW_HOURS = 48


def _episode_code(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


async def notify_arrival(series_id: int, season: int, episode: int) -> int:
    """Push "it's here" to everyone who pinned this series. Returns the count.

    Dedupe is per user: one person's push must not suppress another's for the
    same episode, so episode_notifications is keyed on (user, episode) rather
    than a single notified_at column on the episode.
    """
    settings = get_settings()
    if not settings.notify_on_arrival:
        return 0

    with session() as conn:
        episode_row = conn.execute(
            """
            SELECT e.id AS episode_id, e.title AS episode_title, s.title AS series_title
            FROM episodes e JOIN series s ON s.id = e.series_id
            WHERE e.series_id = ? AND e.season = ? AND e.episode = ?
            """,
            (series_id, season, episode),
        ).fetchone()

        if not episode_row:
            log.debug("no episode row for series=%s %s", series_id, _episode_code(season, episode))
            return 0

        recipients = conn.execute(
            """
            SELECT u.id, u.ntfy_topic
            FROM pins p JOIN users u ON u.id = p.user_id
            WHERE p.series_id = ? AND p.notify = 1
              AND u.ntfy_topic IS NOT NULL AND u.ntfy_topic != ''
              AND NOT EXISTS (
                  SELECT 1 FROM episode_notifications n
                  WHERE n.user_id = u.id AND n.episode_id = ?
              )
            """,
            (series_id, episode_row["episode_id"]),
        ).fetchall()

    if not recipients:
        return 0

    code = _episode_code(season, episode)
    title = f"{episode_row['series_title']} {code} is in Plex"
    body = episode_row["episode_title"] or "Just arrived and ready to watch."

    sent = 0
    for user in recipients:
        if not await ntfy.send(title, body, tags="tv,white_check_mark", topic=user["ntfy_topic"]):
            continue
        # Recorded only on success, so a failed push is retried by reconcile
        # rather than silently swallowed.
        with session() as conn:
            conn.execute(
                "INSERT INTO episode_notifications (user_id, episode_id, notified_at) "
                "VALUES (?, ?, ?) ON CONFLICT(user_id, episode_id) DO NOTHING",
                (user["id"], episode_row["episode_id"], utcnow()),
            )
        sent += 1
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
            SELECT DISTINCT e.series_id, e.season, e.episode
            FROM episodes e JOIN pins p ON p.series_id = e.series_id AND p.notify = 1
            WHERE (e.has_file = 1 OR e.in_plex = 1)
              AND e.arrived_at IS NOT NULL AND e.arrived_at >= ?
              AND EXISTS (
                  SELECT 1 FROM users u
                  WHERE u.id = p.user_id AND u.ntfy_topic IS NOT NULL AND u.ntfy_topic != ''
                    AND NOT EXISTS (
                        SELECT 1 FROM episode_notifications n
                        WHERE n.user_id = u.id AND n.episode_id = e.id
                    )
              )
            ORDER BY e.series_id, e.season, e.episode
            """,
            (cutoff,),
        ).fetchall()

    sent = 0
    for row in pending:
        sent += await notify_arrival(row["series_id"], row["season"], row["episode"])

    # Anything that arrived before the window is marked notified for everyone
    # who could still receive it, so a first run against an existing library
    # doesn't empty the back catalogue onto someone's phone.
    with session() as conn:
        stale = conn.execute(
            """
            INSERT INTO episode_notifications (user_id, episode_id, notified_at)
            SELECT u.id, e.id, ?
            FROM episodes e
            JOIN pins p ON p.series_id = e.series_id
            JOIN users u ON u.id = p.user_id
            WHERE (e.has_file = 1 OR e.in_plex = 1)
              AND (e.arrived_at IS NULL OR e.arrived_at < ?)
            ON CONFLICT(user_id, episode_id) DO NOTHING
            """,
            (utcnow(), cutoff),
        ).rowcount

    return f"{sent} late notification(s) sent, {stale} older arrivals suppressed"


@tracked("weekly_digest")
async def weekly_digest() -> str:
    """One push per user, listing the coming week from their own pin list."""
    settings = get_settings()
    now = datetime.now(UTC)
    end = now + timedelta(days=7)

    with session() as conn:
        users = conn.execute(
            "SELECT id, username, ntfy_topic FROM users "
            "WHERE ntfy_topic IS NOT NULL AND ntfy_topic != ''"
        ).fetchall()

    if not users:
        return "skipped: nobody has an ntfy topic"

    from zoneinfo import ZoneInfo

    local = ZoneInfo(settings.tz)
    sent = 0
    quiet = 0

    for user in users:
        with session() as conn:
            rows = conn.execute(
                """
                SELECT s.title AS series_title, e.season, e.episode, e.air_date_utc
                FROM episodes e
                JOIN series s ON s.id = e.series_id
                JOIN pins p ON p.series_id = s.id AND p.user_id = ?
                WHERE e.air_date_utc IS NOT NULL
                  AND e.air_date_utc >= ? AND e.air_date_utc < ?
                  AND e.season > 0
                ORDER BY e.air_date_utc
                """,
                (user["id"], now.isoformat(), end.isoformat()),
            ).fetchall()

        if not rows:
            # Silence beats "nothing this week" landing every Monday.
            quiet += 1
            continue

        by_day: dict[str, list[str]] = {}
        for row in rows:
            when = datetime.fromisoformat(row["air_date_utc"]).astimezone(local)
            day = when.strftime("%a %d %b")
            code = _episode_code(row["season"], row["episode"])
            by_day.setdefault(day, []).append(f"{row['series_title']} {code}")

        lines = [f"{day}\n  " + "\n  ".join(items) for day, items in by_day.items()]
        if await ntfy.send(
            f"This week: {len(rows)} episode(s)",
            "\n".join(lines),
            tags="tv,calendar",
            topic=user["ntfy_topic"],
        ):
            sent += 1

    return f"digest sent to {sent} user(s), {quiet} had nothing scheduled"
