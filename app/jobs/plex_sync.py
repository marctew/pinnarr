"""Walk the Plex TV libraries and upsert every show."""

from __future__ import annotations

import contextlib
import logging

from app.clients.plex import PlexClient
from app.config import get_settings
from app.db import session, set_setting
from app.jobs import tracked
from app.repo import record_sections, upsert_from_plex

log = logging.getLogger(__name__)


@tracked("plex_library")
async def sync_plex_library() -> str:
    settings = get_settings()
    if not settings.plex_configured:
        return "skipped: Plex not configured"

    client = PlexClient()

    # Names as well as ids: the library facet needs something readable, and
    # we are already making this call.
    available = await client.sections()
    with session() as conn:
        record_sections(conn, available)

    # Cached rather than fetched per page render: it never changes, and a
    # dead Plex shouldn't stop a series page from loading.
    with contextlib.suppress(Exception):
        if machine_id := await client.machine_identifier():
            set_setting("plex_machine_id", machine_id)

    sections = settings.plex_tv_sections
    if not sections:
        # Auto-detect rather than making the user hunt for section IDs.
        sections = [s["id"] for s in available if s["type"] == "show"]
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
