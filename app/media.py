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
import re
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


#: TMDB image paths look like /qJ3q3.jpg and nothing else. Anchored so the
#: only URL this can ever build is one on image.tmdb.org — a path that could
#: carry a slash or a host would turn Pinnarr into an open image proxy.
TMDB_PATH = re.compile(r"^/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp)$")

#: An allowlist, not a passthrough: the size lands in a URL, and "w342 or
#: w185" is the whole range of things a caller legitimately needs. Poster
#: cards get the first, headshots the second — fetching a 342px portrait for
#: a 46px slot is a download nobody sees.
TMDB_SIZES = {"poster": "w342", "face": "w185"}
TMDB_IMAGE = "https://image.tmdb.org/t/p"


async def tmdb_poster(tmdb_id: int, poster_path: str,
                      kind: str = "poster") -> tuple[bytes, str] | None:
    """Artwork for a show Pinnarr has no series row for.

    Proxied and cached like every other poster rather than pointed at from
    the page. Same reason the rest of the app has no CDN in it: the browser
    should not need to reach the internet to draw a page, and a library's
    worth of interests should not be handed to TMDB by every device that
    opens Discover.
    """
    if not poster_path or not TMDB_PATH.match(poster_path):
        return None

    size = TMDB_SIZES.get(kind, TMDB_SIZES["poster"])
    url = f"{TMDB_IMAGE}/{size}{poster_path}"
    # Prefixed so housekeeping's orphan check — which reads a leading series
    # id — sees a name it cannot parse and leaves these alone. They still
    # age out on the usual staleness rule.
    cached = cache_dir() / f"tmdb-{size}-{_key(0, poster_path).split('-', 1)[1]}"
    if cached.exists():
        return cached.read_bytes(), "image/jpeg"

    try:
        content, content_type = await _from_remote(url)
    except Exception as exc:  # noqa: BLE001 — a missing poster is not an outage
        log.warning("TMDB poster fetch failed for %s: %s", tmdb_id, exc)
        return None

    _store(cached, content)
    return content, content_type


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
