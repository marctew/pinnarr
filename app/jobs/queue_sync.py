"""Track Sonarr's download queue.

Refreshed rather than queried per page render: the calendar would otherwise
make a Sonarr call on every load, and be blank whenever Sonarr blinked.

Rows survive across ticks so that progress has a history. Wiping the table
each minute made every download one minute old, which is the same as having
no idea whether one has moved since yesterday.
"""

from __future__ import annotations

import logging

from app.clients.sonarr import SonarrClient
from app.config import get_settings
from app.db import session, utcnow
from app.jobs import tracked

log = logging.getLogger(__name__)


@tracked("sonarr_queue")
async def sync_queue() -> str:
    settings = get_settings()
    if not settings.sonarr_configured:
        return "skipped: Sonarr not configured"

    items = await SonarrClient().queue()
    now = utcnow()

    with session() as conn:
        before = {
            int(r["sonarr_episode_id"]): r
            for r in conn.execute(
                "SELECT sonarr_episode_id, percent, first_seen_at, progress_at "
                "FROM download_queue"
            )
        }
        seen = set()
        for item in items:
            key = int(item.sonarr_episode_id)
            seen.add(key)
            previous = before.get(key)
            # Only a change in percentage counts as progress. Sonarr rewrites
            # the row every poll, so updated_at moves whether or not the
            # download does.
            #
            # A row carried over from before this column existed has no stamp
            # at all. Treating that as "moved just now" starts its clock here
            # rather than leaving it permanently unjudgeable — the alternative
            # is a genuinely stuck download that can never be flagged, because
            # the only thing that would stamp it is the movement it will
            # never make.
            moved = (
                previous is None
                or previous["progress_at"] is None
                or float(previous["percent"]) != float(item.percent)
            )
            conn.execute(
                """
                INSERT INTO download_queue (sonarr_episode_id, status, percent,
                    time_left, message, series_title, episode_title, season,
                    episode, first_seen_at, progress_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sonarr_episode_id) DO UPDATE SET
                    status = excluded.status, percent = excluded.percent,
                    time_left = excluded.time_left, message = excluded.message,
                    series_title = excluded.series_title,
                    episode_title = excluded.episode_title,
                    season = excluded.season, episode = excluded.episode,
                    -- Kept if we have one, filled if we do not: a row that
                    -- predates the column would otherwise stay blank forever.
                    first_seen_at = COALESCE(
                        download_queue.first_seen_at, excluded.first_seen_at
                    ),
                    progress_at = excluded.progress_at,
                    updated_at = excluded.updated_at
                """,
                (key, item.status, item.percent, item.time_left, item.message,
                 item.series_title, item.episode_title, item.season, item.episode,
                 (previous["first_seen_at"] if previous else None) or now,
                 now if moved else (previous["progress_at"] if previous else now),
                 now),
            )

        # Gone from the queue means finished or failed, and either way the
        # stored progress is a lie.
        for key in before.keys() - seen:
            conn.execute(
                "DELETE FROM download_queue WHERE sonarr_episode_id = ?", (key,)
            )

    return f"{len(items)} item(s) downloading"
