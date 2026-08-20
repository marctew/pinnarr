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
        # Plex username → Pinnarr account. A play we cannot attribute is
        # dropped rather than credited to everyone: showing somebody else's
        # viewing as yours is worse than showing none.
        accounts = {
            str(r["plex_username"]).lower(): int(r["id"])
            for r in conn.execute(
                "SELECT id, plex_username FROM users WHERE plex_username IS NOT NULL"
            )
        }

        marked = 0
        unattributed = 0
        not_held = 0
        for play in plays:
            user_id = accounts.get((play.viewer or "").lower())
            if user_id is None:
                unattributed += 1
                continue
            if mark_watched(
                conn, user_id, play.grandparent_rating_key, play.season,
                play.episode, play.watched_at,
            ):
                marked += 1
            else:
                # Yours, but for an episode Pinnarr holds no row for: the
                # episode tables only cover the calendar window plus the full
                # guides of pinned series. Counted, because silently dropping
                # your own history is how "nothing shows as watched" happens
                # with no way to find out why.
                not_held += 1

    note = f"{len(newest or {})} series with history, {marked} episode(s) watched"
    if not_held:
        note += f"; {not_held} of yours are for episodes not synced here"
    if unattributed:
        note += (
            f"; {unattributed} play(s) from Plex accounts nobody here has claimed"
        )
    return note
