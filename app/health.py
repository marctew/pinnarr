"""Connection tests for the admin panel.

These run against the *saved* settings, not whatever is currently typed into
the form — the clients build themselves from app.config, and threading
unsaved values through them would mean a second construction path that could
drift from the real one. Save, then test, is the honest loop.

Every test returns the same shape so the panel can render them uniformly,
and none of them raise: a failed connection is a result, not an error.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients.http import UpstreamError
from app.clients.ntfy import send as ntfy_send
from app.clients.plex import PlexClient
from app.clients.sonarr import SonarrClient
from app.clients.tautulli import TautulliClient
from app.clients.tmdb import TmdbClient
from app.config import get_settings
from app.db import set_setting

log = logging.getLogger(__name__)

SERVICES = ("plex", "sonarr", "tautulli", "tmdb", "ntfy")

#: Legacy Plex agents need different GUID parsing (SPEC §6). Flag them here
#: rather than leaving it to be discovered by a soft-matched library later.
LEGACY_AGENT_PREFIX = "com.plexapp.agents."


def _ok(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "message": message, **extra}


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "message": message}


async def _test_plex() -> dict[str, Any]:
    s = get_settings()
    if not s.plex_configured:
        return _fail("No URL or token saved yet.")

    client = PlexClient()

    # Deep links need the server's own id. Grabbing it here means a failed
    # link is one Test click away from fixed, rather than waiting for 03:00.
    machine_ok = False
    try:
        if machine_id := await client.machine_identifier():
            set_setting("plex_machine_id", machine_id)
            machine_ok = True
    except UpstreamError as exc:
        log.warning("machine identifier unavailable: %s", exc)

    sections = await client.sections()
    shows = [x for x in sections if x["type"] == "show"]
    if not shows:
        return _fail(f"Connected, but none of the {len(sections)} libraries hold TV shows.")

    legacy = [x for x in shows if x["agent"].startswith(LEGACY_AGENT_PREFIX)]
    message = f"Connected. {len(shows)} TV librar{'y' if len(shows) == 1 else 'ies'} found."
    if legacy:
        names = ", ".join(x["title"] for x in legacy)
        message += f" Note: {names} still uses a legacy metadata agent."
    if not machine_ok:
        message += " Could not read the server id, so links into Plex stay hidden."
    return _ok(message, sections=shows)


async def _test_sonarr() -> dict[str, Any]:
    s = get_settings()
    if not s.sonarr_configured:
        return _fail("No URL or API key saved yet.")
    return _ok(f"Connected to Sonarr {await SonarrClient().ping()}.")


async def _test_tautulli() -> dict[str, Any]:
    s = get_settings()
    if not s.tautulli_configured:
        return _fail("No URL or API key saved yet.")
    return _ok(f"Connected to Tautulli {await TautulliClient().ping()}.")


async def _test_tmdb() -> dict[str, Any]:
    s = get_settings()
    if not s.tmdb_configured:
        return _fail("No API key saved yet.")
    return _ok(f"TMDB {await TmdbClient().ping()}.")


async def _test_ntfy() -> dict[str, Any]:
    s = get_settings()
    if not s.ntfy_configured:
        return _fail("No topic saved yet.")
    sent = await ntfy_send(
        "Pinnarr test", "If you can read this, notifications work.", tags="tv,white_check_mark"
    )
    if not sent:
        return _fail(f"ntfy rejected the push to {s.ntfy_topic}.")
    return _ok(f"Test notification pushed to {s.ntfy_topic}. Check your phone.")


_TESTS = {
    "plex": _test_plex,
    "sonarr": _test_sonarr,
    "tautulli": _test_tautulli,
    "tmdb": _test_tmdb,
    "ntfy": _test_ntfy,
}


async def test_service(service: str) -> dict[str, Any]:
    """Run one connection test. Never raises; upstream failures are results."""
    if service not in _TESTS:
        return _fail(f"Unknown service {service!r}.")
    try:
        return await _TESTS[service]()
    except UpstreamError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 — a broken test must not 500 the panel
        log.exception("connection test for %s blew up", service)
        return _fail(f"{type(exc).__name__}: {exc}")
