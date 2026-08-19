"""Pull watch history so the library can be sorted by what you actually watch."""

from __future__ import annotations

import logging

from app.clients.tautulli import TautulliClient
from app.config import get_settings
from app.db import session
from app.jobs import tracked
from app.repo import set_last_watched

log = logging.getLogger(__name__)


@tracked("tautulli_history")
async def sync_tautulli_history() -> str:
    settings = get_settings()
    if not settings.tautulli_configured:
        return "skipped: Tautulli not configured"

    newest = await TautulliClient().last_watched_by_show()
    if not newest:
        return "no history returned"

    with session() as conn:
        for rating_key, watched_at in newest.items():
            set_last_watched(conn, rating_key, watched_at)

    return f"{len(newest)} series with watch history"
