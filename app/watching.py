"""Reading watch state back from Plex on demand, and writing it there.

Both halves of the same conversation, and both deliberately go through Plex
rather than around it.

Plex is authoritative for anyone who has given Pinnarr a personal token: the
hourly sweep records what Plex says and clears what it does not. So marking
something watched *only* in Pinnarr would survive until the next sweep and
then vanish — the write has to reach Plex or it is a lie with a timer on it.
Where there is no token there is no way to tell Plex whose viewing it is,
and the honest answer is to refuse rather than write something that will be
undone within the hour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.clients.http import UpstreamError
from app.clients.plex import PlexClient
from app.config import get_settings
from app.db import session
from app.jobs.watch_state import apply_view_state

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    ok: bool
    detail: str
    marked: int = 0
    cleared: int = 0


def _viewer(user_id: int) -> tuple[str | None, str | None]:
    """This account's Plex token, and why there isn't one if there isn't."""
    if not get_settings().plex_url:
        return None, "Plex isn't configured yet."
    with session() as conn:
        row = conn.execute(
            "SELECT plex_token FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    token = (row["plex_token"] or "").strip() if row else ""
    if not token:
        return None, (
            "You need your own Plex token for this — add one under "
            "Your account. Without it Plex can't be told whose viewing it is, "
            "and anything recorded here would be cleared by the next sync."
        )
    return token, None


def _series_key(series_id: int) -> tuple[str | None, str | None]:
    with session() as conn:
        row = conn.execute(
            "SELECT title, plex_rating_key FROM series WHERE id = ?", (series_id,)
        ).fetchone()
    if row is None:
        return None, "No such series."
    if not row["plex_rating_key"]:
        return None, f"{row['title']} hasn't been matched in Plex yet."
    return str(row["plex_rating_key"]), None


async def refresh(user_id: int, series_id: int) -> Outcome:
    """Re-read one show's watch state from Plex, now.

    The same code the hourly sweep runs, pointed at a single series — so a
    page that looks wrong can be made right without waiting for the top of
    the hour or running a whole-library job.
    """
    token, why = _viewer(user_id)
    if token is None:
        return Outcome(False, why or "No Plex token.")
    key, why = _series_key(series_id)
    if key is None:
        return Outcome(False, why or "Not in Plex.")

    try:
        state = await PlexClient(token=token).view_state(key)
    except UpstreamError as exc:
        return Outcome(False, f"Plex said no: {exc}")

    marked, cleared = apply_view_state(user_id, key, state)
    if not marked and not cleared:
        return Outcome(True, "Already in step with Plex.", 0, 0)
    parts = []
    if marked:
        parts.append(f"{marked} marked watched")
    if cleared:
        parts.append(f"{cleared} cleared")
    return Outcome(True, ", ".join(parts) + ".", marked, cleared)


def episode_keys(series_id: int, season: int | None = None,
                 episode_id: int | None = None) -> list[str]:
    """Plex ids for what is being marked.

    An episode, a season, or nothing — meaning the whole series, for which
    the series key alone is enough because Plex applies it downward.
    """
    clauses = ["series_id = ?", "plex_rating_key IS NOT NULL"]
    args: list = [series_id]
    if episode_id is not None:
        clauses.append("id = ?")
        args.append(episode_id)
    if season is not None:
        clauses.append("season = ?")
        args.append(season)
    with session() as conn:
        return [
            str(r["plex_rating_key"])
            for r in conn.execute(
                f"SELECT plex_rating_key FROM episodes WHERE {' AND '.join(clauses)} "
                "ORDER BY season, episode",
                args,
            )
        ]


async def set_watched(user_id: int, series_id: int, *, watched: bool,
                      season: int | None = None,
                      episode_id: int | None = None) -> Outcome:
    """Tell Plex, then read back what it now says.

    Read-back rather than assumption, for the same reason the watchlist sync
    had to learn: a write believed but not landed becomes, on the next sweep,
    evidence that somebody else changed their mind.
    """
    token, why = _viewer(user_id)
    if token is None:
        return Outcome(False, why or "No Plex token.")
    series_key, why = _series_key(series_id)
    if series_key is None:
        return Outcome(False, why or "Not in Plex.")

    if season is None and episode_id is None:
        # Plex applies a show-level scrobble to everything underneath, so one
        # call does what several hundred would.
        keys = [series_key]
    else:
        keys = episode_keys(series_id, season, episode_id)
        if not keys:
            return Outcome(
                False,
                "Plex hasn't been asked about these episodes yet — refresh "
                "from Plex first, or run the plex_watched job.",
            )

    client = PlexClient(token=token)
    try:
        for key in keys:
            await client.scrobble(key, watched=watched)
        state = await client.view_state(series_key)
    except UpstreamError as exc:
        return Outcome(False, f"Plex said no: {exc}")

    marked, cleared = apply_view_state(user_id, series_key, state)
    what = "watched" if watched else "unwatched"
    return Outcome(True, f"Marked {what} in Plex.", marked, cleared)
