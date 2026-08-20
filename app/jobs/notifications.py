"""Arrival notifications, the reconcile safety net, and the weekly digest."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from app import notify
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
        if not await notify.send(title, body, kind="arrival",
                                 user_id=int(user["id"]), tags="tv,white_check_mark", topic=user["ntfy_topic"]):
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
        if await notify.send(
            f"This week: {len(rows)} episode(s)",
            "\n".join(lines),
            kind="digest",
            user_id=int(user["id"]),
            tags="tv,calendar",
            topic=user["ntfy_topic"],
        ):
            sent += 1

    return f"digest sent to {sent} user(s), {quiet} had nothing scheduled"


def _summarise(series_title: str, episodes: list) -> tuple[str, str]:
    """Turn a batch of arrivals into one notification.

    The useful unit of news is "there is a season to start", not "episode 7
    of 10 finished importing".
    """
    if len(episodes) == 1:
        only = episodes[0]
        code = _episode_code(only["season"], only["episode"])
        return (
            f"{series_title} {code} is in Plex",
            only["episode_title"] or "Just arrived and ready to watch.",
        )

    seasons = {int(e["season"]) for e in episodes}
    codes = ", ".join(_episode_code(e["season"], e["episode"]) for e in episodes)
    if len(seasons) == 1:
        season = next(iter(seasons))
        where = "Specials" if season == 0 else f"Season {season}"
        return (f"{series_title} — {where}, {len(episodes)} episodes", codes)
    return (f"{series_title} — {len(episodes)} episodes just landed", codes)


@tracked("notify_pending")
async def notify_pending() -> str:
    """Push arrivals that have settled, batched per series.

    Runs often and does nothing most of the time. A group is only sent once
    nothing new has arrived for it in the batch window, so an import that
    takes ten minutes still produces one notification rather than one per
    file that happened to land in each tick.
    """
    settings = get_settings()
    if not settings.notify_on_arrival:
        return "skipped: arrival notifications are off"
    if settings.notify_batch_minutes <= 0:
        return "skipped: batching disabled, the webhook pushes directly"

    now = datetime.now(UTC)
    settled_before = (now - timedelta(minutes=settings.notify_batch_minutes)).isoformat()
    floor = (now - timedelta(hours=RECONCILE_WINDOW_HOURS)).isoformat()

    with session() as conn:
        rows = conn.execute(
            """
            SELECT u.id AS user_id, u.ntfy_topic, s.id AS series_id,
                   s.title AS series_title, e.id AS episode_id, e.season, e.episode,
                   e.title AS episode_title, e.arrived_at
            FROM episodes e
            JOIN series s ON s.id = e.series_id
            JOIN pins p ON p.series_id = s.id AND p.notify = 1
            JOIN users u ON u.id = p.user_id
            WHERE (e.has_file = 1 OR e.in_plex = 1)
              AND e.arrived_at IS NOT NULL AND e.arrived_at >= ?
              AND u.ntfy_topic IS NOT NULL AND u.ntfy_topic != ''
              AND NOT EXISTS (
                  SELECT 1 FROM episode_notifications n
                  WHERE n.user_id = u.id AND n.episode_id = e.id
              )
            ORDER BY u.id, s.id, e.season, e.episode
            """,
            (floor,),
        ).fetchall()

    groups: dict[tuple[int, int], list] = {}
    for row in rows:
        groups.setdefault((int(row["user_id"]), int(row["series_id"])), []).append(row)

    sent = 0
    waiting = 0
    for (user_id, _series_id), episodes in groups.items():
        if max(e["arrived_at"] for e in episodes) > settled_before:
            # Still importing. Leave it for a later tick rather than splitting
            # one season across several notifications.
            waiting += 1
            continue

        title, body = _summarise(episodes[0]["series_title"], episodes)
        if not await notify.send(
            title, body, kind="arrival", user_id=user_id, tags="tv,white_check_mark", topic=episodes[0]["ntfy_topic"]
        ):
            continue

        with session() as conn:
            for episode in episodes:
                conn.execute(
                    "INSERT INTO episode_notifications (user_id, episode_id, notified_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(user_id, episode_id) DO NOTHING",
                    (user_id, episode["episode_id"], utcnow()),
                )
        sent += 1

    if not sent and not waiting:
        return "nothing pending"
    return f"{sent} notification(s) sent, {waiting} group(s) still settling"


def _describe_move(old: str | None, new: str | None, tz: str) -> str:
    from zoneinfo import ZoneInfo

    local = ZoneInfo(tz)

    def show(value: str | None) -> str:
        if not value:
            return "no date"
        try:
            when = datetime.fromisoformat(value)
        except ValueError:
            return "no date"
        when = when if when.tzinfo else when.replace(tzinfo=UTC)
        return when.astimezone(local).strftime("%a %d %b")

    return f"{show(old)} → {show(new)}"


@tracked("schedule_changes")
async def notify_schedule_changes() -> str:
    """Tell people when a pinned episode's air date moves.

    Sonarr updates dates quietly and nothing surfaces it, so a finale
    slipping a week is invisible until it fails to turn up. Only genuine
    moves reach this table — see repo._date_moved.
    """
    settings = get_settings()
    cutoff = (datetime.now(UTC) - timedelta(hours=RECONCILE_WINDOW_HOURS)).isoformat()

    with session() as conn:
        rows = conn.execute(
            """
            SELECT c.id AS change_id, c.old_date, c.new_date,
                   e.season, e.episode, s.title AS series_title,
                   u.id AS user_id, u.ntfy_topic
            FROM schedule_changes c
            JOIN episodes e ON e.id = c.episode_id
            JOIN series s ON s.id = e.series_id
            JOIN pins p ON p.series_id = s.id AND p.notify = 1
            JOIN users u ON u.id = p.user_id
            WHERE c.detected_at >= ?
              AND u.ntfy_topic IS NOT NULL AND u.ntfy_topic != ''
              AND NOT EXISTS (
                  SELECT 1 FROM change_notifications n
                  WHERE n.user_id = u.id AND n.change_id = c.id
              )
            ORDER BY c.id
            """,
            (cutoff,),
        ).fetchall()

    sent = 0
    for row in rows:
        code = _episode_code(row["season"], row["episode"])
        ok = await notify.send(
            f"{row['series_title']} {code} has moved",
            _describe_move(row["old_date"], row["new_date"], settings.tz),
            kind="schedule",
            user_id=int(row["user_id"]),
            tags="tv,calendar",
            topic=row["ntfy_topic"],
        )
        if not ok:
            continue
        with session() as conn:
            conn.execute(
                "INSERT INTO change_notifications (user_id, change_id, notified_at) "
                "VALUES (?, ?, ?) ON CONFLICT(user_id, change_id) DO NOTHING",
                (row["user_id"], row["change_id"], utcnow()),
            )
        sent += 1

    return f"{sent} schedule change(s) announced" if sent else "no schedule changes"


#: How far ahead to look for a newly dated season.
SEASON_ALERT_DAYS = 90


@tracked("season_alerts")
async def notify_new_seasons() -> str:
    """Nudge people about shows they own that have picked up a date.

    Discover is a page you have to remember to visit. With 2000 series and a
    dozen pins, the gap between "shows worth following" and "shows you have
    thought to pin" is enormous, and closing it passively is worth more than
    another view nobody opens.
    """
    now = datetime.now(UTC)
    horizon = (now + timedelta(days=SEASON_ALERT_DAYS)).isoformat()

    with session() as conn:
        users = conn.execute(
            "SELECT id, ntfy_topic FROM users "
            "WHERE ntfy_topic IS NOT NULL AND ntfy_topic != ''"
        ).fetchall()

    if not users:
        return "skipped: nobody has an ntfy topic"

    sent = 0
    for user in users:
        with session() as conn:
            rows = conn.execute(
                """
                SELECT s.id, s.title, s.next_airing
                FROM series s
                WHERE s.next_airing IS NOT NULL
                  AND s.next_airing > ? AND s.next_airing < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM pins p WHERE p.series_id = s.id AND p.user_id = ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM season_alerts a
                      WHERE a.user_id = ? AND a.series_id = s.id
                        AND a.next_airing IS s.next_airing
                  )
                ORDER BY s.next_airing
                LIMIT 12
                """,
                (now.isoformat(), horizon, user["id"], user["id"]),
            ).fetchall()

        if not rows:
            continue

        lines = [f"{r['title']} — {r['next_airing'][:10]}" for r in rows]
        ok = await notify.send(
            f"{len(rows)} show(s) in your library have dates",
            "\n".join(lines),
            kind="season",
            user_id=int(user["id"]),
            tags="tv,eyes",
            topic=user["ntfy_topic"],
        )
        if not ok:
            continue

        with session() as conn:
            for row in rows:
                conn.execute(
                    "INSERT INTO season_alerts (user_id, series_id, next_airing, notified_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(user_id, series_id) DO UPDATE SET "
                    "next_airing = excluded.next_airing, notified_at = excluded.notified_at",
                    (user["id"], row["id"], row["next_airing"], utcnow()),
                )
        sent += 1

    return f"{sent} user(s) nudged" if sent else "nothing new to suggest"
