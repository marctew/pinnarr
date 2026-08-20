"""Confirm that episodes are genuinely in Plex, not merely grabbed by Sonarr.

Sonarr's has_file means "the file is on disk where Sonarr put it". That's
usually the same as watchable, but not always — Plex may not have scanned it,
or the library path may be wrong. For pinned shows we care about the stronger
claim, so we ask Plex directly.
"""

from __future__ import annotations

import logging

from app.clients.http import UpstreamError
from app.clients.plex import PlexClient
from app.config import get_settings
from app.db import session, utcnow
from app.jobs import tracked

log = logging.getLogger(__name__)


@tracked("plex_availability")
async def sync_availability() -> str:
    settings = get_settings()
    if not settings.plex_configured:
        return "skipped: Plex not configured"

    with session() as conn:
        pinned = conn.execute(
            "SELECT id, title, plex_rating_key FROM series "
            "WHERE pinned = 1 AND plex_rating_key IS NOT NULL"
        ).fetchall()

    if not pinned:
        return "no pinned series with a Plex key"

    client = PlexClient()
    checked = 0
    newly_present = 0

    for series in pinned:
        try:
            present = await client.episode_keys_present(series["plex_rating_key"])
        except UpstreamError as exc:
            log.warning("availability check failed for %s: %s", series["title"], exc)
            continue

        checked += 1
        with session() as conn:
            episodes = conn.execute(
                "SELECT id, season, episode, in_plex FROM episodes WHERE series_id = ?",
                (series["id"],),
            ).fetchall()
            for ep in episodes:
                is_present = (ep["season"], ep["episode"]) in present
                if is_present == bool(ep["in_plex"]):
                    continue
                conn.execute(
                    "UPDATE episodes SET in_plex = ?, arrived_at = COALESCE(arrived_at, ?), "
                    "updated_at = ? WHERE id = ?",
                    (int(is_present), utcnow() if is_present else None, utcnow(), ep["id"]),
                )
                if is_present:
                    newly_present += 1

            # Stamped only on a successful check, so a Plex outage cannot make
            # a series look like one Plex genuinely does not hold.
            conn.execute(
                "UPDATE series SET plex_checked_at = ? WHERE id = ?",
                (utcnow(), series["id"]),
            )

    return f"{checked}/{len(pinned)} pinned series checked, {newly_present} episodes newly present"
