"""Refresh taste-based suggestions.

TMDB is asked what resembles each pinned show; the answers are kept only
where the library already has them. A dozen calls for a dozen pins, rather
than two thousand for the whole shelf.
"""

from __future__ import annotations

import logging

from app.clients.http import UpstreamError
from app.clients.tmdb import TmdbClient
from app.config import get_settings
from app.db import session
from app.jobs import tracked
from app.repo import store_recommendations

log = logging.getLogger(__name__)


@tracked("suggestions")
async def refresh_suggestions() -> str:
    if not get_settings().tmdb_configured:
        return "skipped: TMDB not configured"

    with session() as conn:
        pinned = conn.execute(
            "SELECT DISTINCT s.id, s.title, s.tmdb_id FROM series s "
            "JOIN pins p ON p.series_id = s.id WHERE s.tmdb_id IS NOT NULL"
        ).fetchall()

    if not pinned:
        return "nothing pinned with a TMDB id"

    client = TmdbClient()
    done = 0
    for series in pinned:
        try:
            items = await client.recommendations(int(series["tmdb_id"]))
        except UpstreamError as exc:
            log.warning("recommendations failed for %s: %s", series["title"], exc)
            continue
        with session() as conn:
            store_recommendations(conn, int(series["id"]), items)
        done += 1

    return f"{done}/{len(pinned)} pinned series refreshed"
