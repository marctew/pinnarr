"""The Plex Watchlist.

Not on your server. The watchlist lives in Plex's cloud, attached to an
account rather than to a Plex Media Server, so this talks to a different host
entirely and keeps working when the server is off.

Plex publishes no stable API for it. These endpoints are well-trodden — other
tools in this space use exactly the same ones — but they are reverse
engineered and could change without notice, which is a different risk from
Sonarr's versioned API. Every call is written to fail softly for that reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.clients.http import UpstreamError, request_json

log = logging.getLogger(__name__)

DISCOVER = "https://discover.provider.plex.tv"

SERVICE = "plex watchlist"


def _headers(token: str) -> dict[str, str]:
    # Plex is fussy about anonymous clients on these endpoints, so identify
    # ourselves properly rather than sending a bare token.
    return {
        "X-Plex-Token": token,
        "X-Plex-Client-Identifier": "pinnarr",
        "X-Plex-Product": "Pinnarr",
        "Accept": "application/json",
    }


@dataclass
class WatchlistItem:
    rating_key: str
    guid: str
    title: str
    kind: str


async def fetch(token: str) -> list[WatchlistItem]:
    """Everything on the account's watchlist.

    Films are filtered out here rather than later: Pinnarr is TV only until
    the Radarr work lands, and a film on a pin list would be a puzzle.
    """
    if not token:
        raise UpstreamError(SERVICE, "no Plex token for this account")

    data = await request_json(
        SERVICE,
        "GET",
        f"{DISCOVER}/library/sections/watchlist/all",
        headers=_headers(token),
        params={"includeCollections": 0, "includeExternalMedia": 1},
    )
    items = ((data or {}).get("MediaContainer") or {}).get("Metadata") or []

    out: list[WatchlistItem] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").lower() != "show":
            continue
        guid = str(item.get("guid") or "")
        rating_key = str(item.get("ratingKey") or "")
        if not guid or not rating_key:
            continue
        out.append(
            WatchlistItem(
                rating_key=rating_key,
                guid=guid,
                title=str(item.get("title") or "untitled"),
                kind="show",
            )
        )
    return out


async def _act(token: str, action: str, rating_key: str) -> None:
    await request_json(
        SERVICE,
        "PUT",
        f"{DISCOVER}/actions/{action}",
        headers=_headers(token),
        params={"ratingKey": rating_key},
    )


async def add(token: str, rating_key: str) -> None:
    await _act(token, "addToWatchlist", rating_key)


async def remove(token: str, rating_key: str) -> None:
    await _act(token, "removeFromWatchlist", rating_key)


def rating_key_from_guid(guid: str | None) -> str | None:
    """plex://show/5d9c0874ffd9ef001e99607a → 5d9c0874ffd9ef001e99607a.

    The Discover rating key is the tail of the plex:// identity, so a series
    already matched in Plex needs no lookup to be watchlisted.
    """
    if not guid or not guid.startswith("plex://"):
        return None
    tail = guid.rsplit("/", 1)[-1].strip()
    return tail or None


async def check(token: str) -> dict[str, Any]:
    """Confirm a token works, for the profile page."""
    try:
        items = await fetch(token)
    except UpstreamError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 — an undocumented API deserves this
        log.warning("watchlist check failed: %s", exc)
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "message": f"Connected. {len(items)} TV show(s) watchlisted."}
