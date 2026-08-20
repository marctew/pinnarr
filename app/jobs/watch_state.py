"""Read watched state from Plex, per user.

Tautulli logs plays; Plex holds the truth. They diverge whenever somebody
marks an episode watched without playing it — a normal thing to do, and one
that produces no history at all, so a Tautulli-only view would never see it.

Two passes, for the same reason the Tautulli sync has two. The frequent one
covers pinned shows, which is what anyone is actually looking at, and costs a
request per show. The nightly one sweeps whole library sections in a handful
of paged requests, which is the only affordable way to keep two thousand
series honest.

Plex is authoritative for whatever it returns: watched episodes are recorded
and unwatched ones cleared. Adding only would let a stale play record outlive
the state it came from, and nothing could ever become un-watched.

Because clearing is unconditional, this job has to be the only one writing
watch rows for the people it covers — which is anyone who has supplied a
personal token. The Tautulli sync steps aside for them and handles everyone
else. Two authorities over one row is not a merge, it is a flicker.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients.plex import PlexClient
from app.config import get_settings
from app.db import session, utcnow
from app.jobs import tracked
from app.repo import mark_watched, set_episode_plex_key, unmark_watched

log = logging.getLogger(__name__)


def _viewers() -> list[Any]:
    with session() as conn:
        return list(
            conn.execute(
                "SELECT id, username, plex_token FROM users "
                "WHERE plex_token IS NOT NULL AND plex_token != ''"
            )
        )


def _apply(user_id: int, series_key: str, state: dict) -> tuple[int, int]:
    marked = cleared = 0
    with session() as conn:
        for (season, episode), view in state.items():
            if view.rating_key:
                set_episode_plex_key(conn, series_key, season, episode, view.rating_key)
            if view.watched:
                # Plex's own timestamp where it has one. Stamping "now" dated
                # every watch to whenever the sweep ran, which made the
                # library's "last watched" column a clock, not a history.
                if mark_watched(conn, user_id, series_key, season, episode,
                                view.viewed_at or utcnow(), source="plex"):
                    marked += 1
            elif unmark_watched(conn, user_id, series_key, season, episode):
                cleared += 1
    return marked, cleared


@tracked("plex_watched")
async def sync_watch_state() -> str:
    """Pinned shows, often. One request each, and it is what you are looking at."""
    if not get_settings().plex_url:
        return "skipped: Plex not configured"

    viewers = _viewers()
    if not viewers:
        return "skipped: nobody has a Plex token"

    notes = []
    for viewer in viewers:
        with session() as conn:
            series = list(
                conn.execute(
                    "SELECT s.title, s.plex_rating_key FROM series s "
                    "JOIN pins p ON p.series_id = s.id AND p.user_id = ? "
                    "WHERE s.plex_rating_key IS NOT NULL",
                    (viewer["id"],),
                )
            )

        client = PlexClient(viewer["plex_token"])
        marked = cleared = failed = 0
        for show in series:
            try:
                state = await client.view_state(show["plex_rating_key"])
            except Exception as exc:  # noqa: BLE001 — one bad show, not the run
                log.warning("watch state failed for %s: %s", show["title"], exc)
                failed += 1
                continue
            got, lost = _apply(int(viewer["id"]), show["plex_rating_key"], state)
            marked += got
            cleared += lost

        note = f"{viewer['username']}: {marked} watched across {len(series)} pin(s)"
        if cleared:
            note += f", {cleared} corrected"
        if failed:
            note += f", {failed} series Plex would not answer for"
        notes.append(note)

    return "; ".join(notes)


@tracked("plex_watched_full")
async def sync_all_watch_state() -> str:
    """Every series in the library, nightly.

    Swept by section rather than by show: two thousand round trips a night to
    keep the badges honest on shows nobody has pinned would be absurd, and
    Plex will hand over a whole section in a few pages.
    """
    if not get_settings().plex_url:
        return "skipped: Plex not configured"

    viewers = _viewers()
    if not viewers:
        return "skipped: nobody has a Plex token"

    with session() as conn:
        sections = [int(r["id"]) for r in conn.execute("SELECT id FROM plex_sections")]
        known = {
            str(r["plex_rating_key"])
            for r in conn.execute(
                "SELECT plex_rating_key FROM series WHERE plex_rating_key IS NOT NULL"
            )
        }

    if not sections:
        return "no Plex sections recorded — run plex_library first"

    notes = []
    for viewer in viewers:
        client = PlexClient(viewer["plex_token"])
        marked = cleared = seen_series = 0

        for section in sections:
            try:
                by_series = await client.section_view_state(section)
            except Exception as exc:  # noqa: BLE001 — a section, not the run
                log.warning("section %s sweep failed: %s", section, exc)
                continue

            for series_key, state in by_series.items():
                if series_key not in known:
                    continue
                seen_series += 1
                got, lost = _apply(int(viewer["id"]), series_key, state)
                marked += got
                cleared += lost

        note = f"{viewer['username']}: {marked} watched across {seen_series} series"
        if cleared:
            note += f", {cleared} corrected"
        notes.append(note)

    return "; ".join(notes)
