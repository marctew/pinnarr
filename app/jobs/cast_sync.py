"""Pull the cast of pinned shows from TMDB.

Pinned only, and deliberately so. Two thousand series is two thousand calls
for a nightly job, and the question this answers — "where do I know them
from" — is asked while looking at something you follow. The other direction
still works: an unpinned show turns up in a person's list as soon as
anything you *have* pinned shares a face with it.
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


@tracked("tmdb_cast")
async def sync_cast() -> str:
    if not get_settings().tmdb_configured:
        return "skipped: TMDB not configured"

    with session() as conn:
        targets = list(
            conn.execute(
                "SELECT DISTINCT s.id, s.tmdb_id, s.title FROM series s "
                "JOIN pins p ON p.series_id = s.id "
                "WHERE s.tmdb_id IS NOT NULL ORDER BY s.sort_title"
            )
        )

    if not targets:
        return "nothing pinned with a TMDB id"

    client = TmdbClient()
    done = 0
    people = 0
    failed = 0

    for row in targets:
        try:
            cast = await client.credits(int(row["tmdb_id"]), limit=CAST_LIMIT)
        except UpstreamError as exc:
            # One show's credits are not worth the whole run.
            log.warning("no cast for %s: %s", row["title"], exc)
            failed += 1
            continue

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
                people += 1
        done += 1

    note = f"{done}/{len(targets)} pinned show(s), {people} credit(s)"
    if failed:
        note += f", {failed} failed"
    return note
