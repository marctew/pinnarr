"""Read watched state from Plex, per user.

Tautulli logs plays; Plex holds the truth. They diverge whenever somebody
marks an episode watched without playing it — a normal thing to do, and one
that produces no history at all, so a Tautulli-only view would never see it.

Scoped to pinned series, because that is what any of this is for and because
it is one request per show per user.
"""

from __future__ import annotations

import logging

from app.clients.http import UpstreamError
from app.clients.plex import PlexClient
from app.config import get_settings
from app.db import session, utcnow
from app.jobs import tracked
from app.repo import mark_watched

log = logging.getLogger(__name__)


@tracked("plex_watched")
async def sync_watch_state() -> str:
    if not get_settings().plex_url:
        return "skipped: Plex not configured"

    with session() as conn:
        viewers = list(
            conn.execute(
                "SELECT id, username, plex_token FROM users "
                "WHERE plex_token IS NOT NULL AND plex_token != ''"
            )
        )

    if not viewers:
        return "skipped: nobody has a Plex token"

    notes = []
    for viewer in viewers:
        with session() as conn:
            series = list(
                conn.execute(
                    "SELECT s.id, s.title, s.plex_rating_key FROM series s "
                    "JOIN pins p ON p.series_id = s.id AND p.user_id = ? "
                    "WHERE s.plex_rating_key IS NOT NULL",
                    (viewer["id"],),
                )
            )

        client = PlexClient(viewer["plex_token"])
        marked = 0
        for show in series:
            try:
                seen = await client.watched_episodes(show["plex_rating_key"])
            except UpstreamError as exc:
                log.warning("watch state failed for %s: %s", show["title"], exc)
                continue
            with session() as conn:
                for season, episode in seen:
                    if mark_watched(
                        conn, int(viewer["id"]), show["plex_rating_key"],
                        season, episode, utcnow(),
                    ):
                        marked += 1
        notes.append(f"{viewer['username']}: {marked} watched across {len(series)} pin(s)")

    return "; ".join(notes)
