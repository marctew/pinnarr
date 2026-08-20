"""Track Sonarr's download queue.

Refreshed rather than queried per page render: the calendar would otherwise
make a Sonarr call on every load, and be blank whenever Sonarr blinked.
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
        # Replaced wholesale: an item that has left the queue has finished or
        # failed, and either way the old progress is a lie.
        conn.execute("DELETE FROM download_queue")
        for item in items:
            conn.execute(
                "INSERT INTO download_queue (sonarr_episode_id, status, percent, "
                "time_left, message, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sonarr_episode_id) DO UPDATE SET status = excluded.status, "
                "percent = excluded.percent, time_left = excluded.time_left, "
                "message = excluded.message, updated_at = excluded.updated_at",
                (item.sonarr_episode_id, item.status, item.percent,
                 item.time_left, item.message, now),
            )

    return f"{len(items)} item(s) downloading"
