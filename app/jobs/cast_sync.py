"""Pull cast lists from TMDB, so a face can be traced across your own shelf.

Two passes, and the second one is the point.

Pinned shows are refreshed every night: they are few, and they are what you
are looking at. But the cross-reference needs credits on *both* series, so
covering only pins means "you have seen them before" can only ever fire
between two things you already follow — while the show that would actually
answer the question is the one you watched three years ago and never pinned.

So everything else gets backfilled too, a batch a night, most-watched first.
Two thousand calls in one go would be rude to a free API; two thousand calls
spread over a week is nothing.
"""

from __future__ import annotations

import logging

from app.clients.http import UpstreamError
from app.clients.tmdb import TmdbClient
from app.config import get_settings
from app.db import session, utcnow
from app.jobs import tracked

log = logging.getLogger(__name__)

#: Beyond this the names stop being ones you would recognise, and each extra
#: row makes "also in" noisier rather than richer.
CAST_LIMIT = 20

#: How many never-fetched series to add per run. At one run a night a two
#: thousand series library is covered inside a week, and the manual trigger
#: on /settings/jobs will chew through it faster if you are impatient.
BACKFILL_PER_RUN = 300

#: Pinned credits are re-fetched this often. Recasts and corrections happen,
#: but not weekly.
REFRESH_DAYS = 30


def _uncovered(conn) -> int:
    return int(
        conn.execute(
            "SELECT count(*) AS n FROM series "
            "WHERE tmdb_id IS NOT NULL AND cast_synced_at IS NULL"
        ).fetchone()["n"]
    )


def _targets(conn, limit: int) -> tuple[list, list]:
    """Pinned shows due a refresh, then a batch of never-fetched ones.

    Backfill order is by how much of the show you have actually watched:
    those are the ones whose faces you would recognise, so they are the ones
    that make the feature work soonest.
    """
    stale = list(
        conn.execute(
            """
            SELECT DISTINCT s.id, s.tmdb_id, s.title
            FROM series s
            JOIN pins p ON p.series_id = s.id
            WHERE s.tmdb_id IS NOT NULL
              AND (
                  s.cast_synced_at IS NULL
                  OR s.cast_synced_at < datetime('now', ?)
              )
            ORDER BY s.sort_title
            """,
            (f"-{REFRESH_DAYS} days",),
        )
    )

    backfill = list(
        conn.execute(
            """
            SELECT s.id, s.tmdb_id, s.title,
                   (
                       SELECT count(*) FROM episodes e
                       JOIN episode_watches w ON w.episode_id = e.id
                       WHERE e.series_id = s.id
                   ) AS seen
            FROM series s
            WHERE s.tmdb_id IS NOT NULL AND s.cast_synced_at IS NULL
              AND NOT EXISTS (SELECT 1 FROM pins p WHERE p.series_id = s.id)
            ORDER BY seen DESC, s.in_plex DESC, s.sort_title
            LIMIT ?
            """,
            (limit,),
        )
    )

    return stale, backfill


async def _fetch_one(client: TmdbClient, row) -> bool:
    try:
        cast = await client.credits(int(row["tmdb_id"]), limit=CAST_LIMIT)
    except UpstreamError as exc:
        # One show's credits are not worth the whole run.
        log.warning("no cast for %s: %s", row["title"], exc)
        return False

    with session() as conn:
        # Replaced rather than merged: a recast or a correction upstream
        # should not leave the old name behind for ever.
        conn.execute("DELETE FROM series_cast WHERE series_id = ?", (row["id"],))
        for member in cast:
            conn.execute(
                "INSERT INTO people (tmdb_person_id, name, profile_path, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(tmdb_person_id) DO UPDATE SET "
                "name = excluded.name, profile_path = excluded.profile_path, "
                "updated_at = excluded.updated_at",
                (member.tmdb_person_id, member.name, member.profile_path, utcnow()),
            )
            conn.execute(
                "INSERT INTO series_cast (series_id, tmdb_person_id, character, "
                "episode_count, billing) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(series_id, tmdb_person_id) DO UPDATE SET "
                "character = excluded.character, "
                "episode_count = excluded.episode_count, billing = excluded.billing",
                (row["id"], member.tmdb_person_id, member.character,
                 member.episode_count, member.billing),
            )
        # Stamped even when a show has no cast listed, so an empty answer is
        # not re-asked every night for ever.
        conn.execute(
            "UPDATE series SET cast_synced_at = ? WHERE id = ?", (utcnow(), row["id"])
        )
    return True


@tracked("tmdb_cast")
async def sync_cast(backfill: int = BACKFILL_PER_RUN) -> str:
    if not get_settings().tmdb_configured:
        return "skipped: TMDB not configured"

    with session() as conn:
        stale, fresh = _targets(conn, backfill)

    if not stale and not fresh:
        return "in step"

    client = TmdbClient()
    refreshed = sum([await _fetch_one(client, row) for row in stale])
    filled = sum([await _fetch_one(client, row) for row in fresh])
    failed = (len(stale) + len(fresh)) - (refreshed + filled)

    # Counted afterwards rather than deduced: the pinned pass covers some of
    # what the backfill was going to, and arithmetic on a stale total gets
    # that wrong in exactly the way that makes a progress figure untrustworthy.
    with session() as conn:
        left = _uncovered(conn)

    note = f"{refreshed} pinned refreshed, {filled} backfilled"
    if failed:
        note += f", {failed} failed"
    note += f", {left} left to cover" if left else ", whole library covered"
    return note
