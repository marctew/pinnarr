"""Refresh the full episode guide for every pinned series.

The per-series button is the right shape for browsing — nobody wants every
episode of two thousand shows — but it makes fixing a whole pin list a chore.
This does the same work for the shows you actually follow.
"""

from __future__ import annotations

import logging

from app.clients.http import UpstreamError
from app.clients.sonarr import SonarrClient
from app.config import get_settings
from app.db import session
from app.jobs import tracked
from app.repo import mark_episodes_synced, upsert_episode

log = logging.getLogger(__name__)


@tracked("pinned_guides")
async def refresh_pinned_guides() -> str:
    if not get_settings().sonarr_configured:
        return "skipped: Sonarr not configured"

    with session() as conn:
        pinned = conn.execute(
            "SELECT DISTINCT s.id, s.title, s.sonarr_id, s.tmdb_id FROM series s "
            "JOIN pins p ON p.series_id = s.id WHERE s.sonarr_id IS NOT NULL"
        ).fetchall()

    if not pinned:
        return "nothing pinned that Sonarr tracks"

    client = SonarrClient()
    done = 0
    episodes_seen = 0

    for series in pinned:
        try:
            episodes = await client.episodes_for_series(int(series["sonarr_id"]))
        except UpstreamError as exc:
            log.warning("guide refresh failed for %s: %s", series["title"], exc)
            continue
        with session() as conn:
            for episode in episodes:
                upsert_episode(conn, int(series["id"]), episode)
            mark_episodes_synced(conn, int(series["id"]))
        done += 1
        episodes_seen += len(episodes)

    return f"{done}/{len(pinned)} pinned series, {episodes_seen} episode(s)"
