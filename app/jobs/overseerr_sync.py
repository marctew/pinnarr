"""What Overseerr has already been asked for.

Cached rather than asked per card. A Discover page showing two dozen
suggestions would otherwise be two dozen calls to draw one screen, and every
one of them would be asking the same question a sweep can answer once.
"""

from __future__ import annotations

import logging

from app.clients.http import UpstreamError
from app.clients.overseerr import OverseerrClient
from app.config import get_settings
from app.db import session
from app.jobs import tracked
from app.repo import store_media_states

log = logging.getLogger(__name__)


@tracked("overseerr_requests")
async def sync_requests() -> str:
    settings = get_settings()
    if not settings.overseerr_requests_enabled:
        return "skipped: Overseerr needs a URL and an API key for this"

    try:
        states = await OverseerrClient().media_states()
    except UpstreamError as exc:
        return f"error: {exc}"

    with session() as conn:
        stored = store_media_states(conn, states)

    waiting = sum(1 for s in states.values() if s.status in ("pending", "processing"))
    return f"{stored} request(s) known, {waiting} still on their way"
