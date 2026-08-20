"""Read watched state from Plex, per user.

Tautulli logs plays; Plex holds the truth. They diverge whenever somebody
marks an episode watched without playing it — a normal thing to do, and one
that produces no history at all, so a Tautulli-only view would never see it.

Scoped to pinned series, because that is what any of this is for and because
it is one request per show per user.
"""

from __future__ import annotations

import logging

from app.clients.plex import PlexClient
from app.config import get_settings
from app.db import session, utcnow
from app.jobs import tracked
from app.repo import mark_watched, unmark_watched

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
        cleared = 0
        failed = 0

        for show in series:
            try:
                state = await client.view_state(show["plex_rating_key"])
            except Exception as exc:  # noqa: BLE001 — one bad show, not the run
                log.warning("watch state failed for %s: %s", show["title"], exc)
                failed += 1
                continue

            # Plex is authoritative for what it returns. Adding only would let
            # a stale Tautulli play record outlive the state it came from, and
            # nothing could ever be un-watched.
            with session() as conn:
                for (season, episode), watched in state.items():
                    if watched:
                        if mark_watched(
                            conn, int(viewer["id"]), show["plex_rating_key"],
                            season, episode, utcnow(),
                        ):
                            marked += 1
                    elif unmark_watched(
                        conn, int(viewer["id"]), show["plex_rating_key"], season, episode
                    ):
                        cleared += 1

        note = f"{viewer['username']}: {marked} watched across {len(series)} pin(s)"
        if cleared:
            note += f", {cleared} corrected"
        if failed:
            note += f", {failed} series Plex would not answer for"
        notes.append(note)

    return "; ".join(notes)
