"""Poster caching.

Plex will happily serve a poster, but only with X-Plex-Token attached, and
that token grants full read access to the library. Putting it in an <img src>
would print it into the page source of every library view. So posters are
fetched server-side, cached on disk next to the database, and served from
here — the token never leaves the process.

Not every series is in Plex. A good fraction of a typical library is tracked
by Sonarr and not downloaded yet, and Discover is made almost entirely of
those, so Sonarr's own artwork is the fallback. It is an absolute public URL,
which means no credentials to attach and no dependency on Sonarr being up.

The cache key includes the source URL, so changed artwork produces a new key
and the stale file is simply never asked for again.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import httpx

from app.clients.http import DEFAULT_TIMEOUT, UpstreamError
from app.clients.plex import PlexClient
from app.config import get_bootstrap

log = logging.getLogger(__name__)

#: Refuse anything implausible for a poster, so a misconfigured URL cannot
#: quietly fill the disk.
MAX_BYTES = 8 * 1024 * 1024


def cache_dir() -> Path:
    path = Path(get_bootstrap().database_path).parent / "posters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key(series_id: int, source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return f"{series_id}-{digest}"


def _store(cached: Path, content: bytes) -> None:
    # Write via a temp name so a torn write can't be served as a valid poster.
    tmp = cached.with_suffix(".part")
    tmp.write_bytes(content)
    tmp.replace(cached)


async def _from_remote(url: str) -> tuple[bytes, str]:
    """Fetch artwork from an absolute URL — Sonarr's remoteUrl, which usually
    points at TVDB. No credentials are attached, deliberately."""
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url)
    resp.raise_for_status()
    if len(resp.content) > MAX_BYTES:
        raise UpstreamError("poster", f"{url} returned {len(resp.content)} bytes")
    return resp.content, resp.headers.get("Content-Type", "image/jpeg")


async def poster(
    series_id: int, *, plex_thumb: str = "", remote_url: str = ""
) -> tuple[bytes, str] | None:
    """Poster bytes for a series, from cache or upstream. None if unavailable.

    Plex first: it is on the LAN, and its artwork is the one you chose. Sonarr's
    remote art is the fallback for anything not downloaded yet. A failure in
    the first source falls through to the second rather than giving up — a Plex
    hiccup should not blank a whole page of cards.
    """
    sources: list[tuple[str, str]] = []
    if plex_thumb:
        sources.append(("plex", plex_thumb))
    if remote_url:
        sources.append(("remote", remote_url))

    for kind, source in sources:
        cached = cache_dir() / _key(series_id, source)
        if cached.exists():
            return cached.read_bytes(), "image/jpeg"

        try:
            if kind == "plex":
                content, content_type = await PlexClient().fetch_poster(source)
            else:
                content, content_type = await _from_remote(source)
        except Exception as exc:  # noqa: BLE001 — a missing poster is not an outage
            log.warning("poster fetch failed (%s) for series %s: %s", kind, series_id, exc)
            continue

        _store(cached, content)
        return content, content_type

    return None
