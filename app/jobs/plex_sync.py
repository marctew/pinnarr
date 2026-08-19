"""Walk the Plex TV libraries and upsert every show."""

from __future__ import annotations

import logging

from app.clients.plex import PlexClient
from app.config import get_settings
from app.db import session
from app.jobs import tracked
from app.repo import upsert_from_plex

log = logging.getLogger(__name__)


@tracked("plex_library")
async def sync_plex_library() -> str:
    settings = get_settings()
    if not settings.plex_configured:
        return "skipped: Plex not configured"

    client = PlexClient()

    sections = settings.plex_tv_sections
    if not sections:
        # Auto-detect rather than making the user hunt for section IDs.
        sections = await client.tv_section_ids()
        log.info("auto-detected TV sections: %s", sections)
    if not sections:
        return "no TV sections found"

    total = 0
    unmatched = 0
    for section_id in sections:
        shows = await client.shows(section_id)
        with session() as conn:
            for show in shows:
                upsert_from_plex(conn, show)
                if not show.has_external_id:
                    unmatched += 1
        total += len(shows)
        log.info("section %s: %d shows", section_id, len(shows))

    detail = f"{total} shows across {len(sections)} section(s)"
    if unmatched:
        detail += f"; {unmatched} without any external id (will match on title+year)"
    return detail
