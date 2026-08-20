"""Pull watch history so the library can be sorted by what you actually watch."""

from __future__ import annotations

import logging

from app.clients.tautulli import TautulliClient
from app.config import get_settings
from app.db import session
from app.jobs import tracked
from app.repo import mark_watched, set_last_watched

log = logging.getLogger(__name__)


@tracked("tautulli_history")
async def sync_tautulli_history() -> str:
    settings = get_settings()
    if not settings.tautulli_configured:
        return "skipped: Tautulli not configured"

    client = TautulliClient()
    newest = await client.last_watched_by_show()
    plays = await client.watched_episodes()

    with session() as conn:
        for rating_key, watched_at in (newest or {}).items():
            set_last_watched(conn, rating_key, watched_at)

        # Per episode as well as per series. Without this, Ready to Watch
        # could never strike anything off — marking something watched in Plex
        # changed a sort order and nothing else.
        marked = 0
        for play in plays:
            if mark_watched(
                conn, play.grandparent_rating_key, play.season, play.episode,
                play.watched_at,
            ):
                marked += 1

    return f"{len(newest or {})} series with history, {marked} episode(s) marked watched"
