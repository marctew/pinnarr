"""Pull watch history so the library can be sorted by what you actually watch.

Two things come out of this, and only one of them is watch state.

Per series, `last_watched_at` drives the library's "recently watched" sort.
That is for everybody, always.

Per episode, plays become watch rows — but only for accounts that have *not*
given Pinnarr a personal Plex token. Where there is a token, Plex answers
directly, and it is authoritative: the watch_state sweep clears anything Plex
reports unwatched. Marking here too meant the two jobs took turns adding and
deleting the same rows every hour. Exactly one source speaks per person.
"""

from __future__ import annotations

import logging

from app.clients.tautulli import TautulliClient
from app.config import get_settings
from app.db import session
from app.jobs import tracked
from app.repo import mark_watched, set_last_watched

log = logging.getLogger(__name__)


RECENT_PLAYS = 200
ALL_PLAYS = 5000


@tracked("tautulli_recent")
async def sync_recent_history() -> str:
    """The last couple of hundred plays. Cheap enough to run hourly, which is
    what makes watching something show up the same evening."""
    return await _sync(RECENT_PLAYS)


@tracked("tautulli_history")
async def sync_tautulli_history() -> str:
    """The full sweep, nightly: catches anything the hourly pass missed and
    any history that arrived out of order."""
    return await _sync(ALL_PLAYS)


async def _sync(length: int) -> str:
    settings = get_settings()
    if not settings.tautulli_configured:
        return "skipped: Tautulli not configured"

    client = TautulliClient()
    newest = await client.last_watched_by_show()
    plays = await client.watched_episodes(length=length)

    with session() as conn:
        for rating_key, watched_at in (newest or {}).items():
            set_last_watched(conn, rating_key, watched_at)

        # Per episode as well as per series. Without this, Ready to Watch
        # could never strike anything off — marking something watched in Plex
        # changed a sort order and nothing else.
        # Plex username → Pinnarr account. A play we cannot attribute is
        # dropped rather than credited to everyone: showing somebody else's
        # viewing as yours is worse than showing none.
        #
        # Anyone with a personal Plex token is left out. Plex speaks for them
        # directly, and it is authoritative: the watch_state sweep clears
        # whatever Plex reports unwatched. Marking here as well meant these
        # two jobs took turns adding and deleting the same rows, on the hour,
        # forever. One source per person, and the better one wins.
        accounts = {}
        deferred = 0
        for r in conn.execute(
            "SELECT id, plex_username, plex_token FROM users "
            "WHERE plex_username IS NOT NULL"
        ):
            if (r["plex_token"] or "").strip():
                deferred += 1
                continue
            accounts[str(r["plex_username"]).lower()] = int(r["id"])

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
                play.episode, play.watched_at, source="tautulli",
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
    if deferred:
        note += f"; {deferred} account(s) left to Plex, which speaks for them directly"
    return note
