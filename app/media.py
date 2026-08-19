"""Poster caching.

Plex will happily serve a poster, but only with X-Plex-Token attached, and
that token grants full read access to the library. Putting it in an <img src>
would print it into the page source of every library view. So posters are
fetched server-side, cached on disk next to the database, and served from
here — the token never leaves the process.

The cache key includes the thumb path, so Plex changing a show's artwork
produces a new key and the stale file is simply never asked for again.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from app.clients.http import UpstreamError
from app.clients.plex import PlexClient
from app.config import get_bootstrap

log = logging.getLogger(__name__)


def cache_dir() -> Path:
    path = Path(get_bootstrap().database_path).parent / "posters"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key(series_id: int, thumb: str) -> str:
    digest = hashlib.sha256(thumb.encode("utf-8")).hexdigest()[:16]
    return f"{series_id}-{digest}"


async def poster(series_id: int, thumb: str) -> tuple[bytes, str] | None:
    """Poster bytes for a series, from cache or Plex. None if unavailable."""
    if not thumb:
        return None

    cached = cache_dir() / _key(series_id, thumb)
    if cached.exists():
        return cached.read_bytes(), "image/jpeg"

    try:
        content, content_type = await PlexClient().fetch_poster(thumb)
    except (UpstreamError, OSError) as exc:
        log.warning("poster fetch failed for series %s: %s", series_id, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — a missing poster is not an outage
        log.warning("poster fetch failed for series %s: %s", series_id, exc)
        return None

    # Write via a temp name so a torn write can't be served as a valid poster.
    tmp = cached.with_suffix(".part")
    tmp.write_bytes(content)
    tmp.replace(cached)
    return content, content_type
