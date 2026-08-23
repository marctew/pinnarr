"""Overseerr client — owns "can I have this?" for things you do not own.

Everything else in the stack answers questions about what is already on the
shelf. This is the only one that can do something about what is not, which
is why Pinnarr can now suggest a show it has never seen a file of.

The key is a single admin credential, so a request made with it is
attributed to whoever owns it unless a userId is supplied. Pinnarr has real
accounts, so it supplies one — Kate asking for something should not appear
in Overseerr as you.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.clients.http import UpstreamError, request_json
from app.config import get_settings

log = logging.getLogger(__name__)

#: Overseerr's media status enum. Numbers rather than names on the wire, and
#: undocumented in the responses themselves, so they are written down here.
STATUS = {
    1: "unknown",
    2: "pending",
    3: "processing",
    4: "partly available",
    5: "available",
    6: "blacklisted",
    7: "deleted",
}

#: What reads as "you already asked for this", for a button that should not
#: offer to ask again.
REQUESTED = frozenset({"pending", "processing", "partly available", "available"})


@dataclass
class OverseerrUser:
    user_id: int
    name: str
    email: str | None


@dataclass
class MediaState:
    tmdb_id: int
    status: str
    #: Who asked, where Overseerr says. Absent for media it knows about but
    #: nobody requested — imported from the library, usually.
    requested_by: str | None = None


class OverseerrClient:
    service = "overseerr"

    def __init__(self) -> None:
        s = get_settings()
        self.base = s.overseerr_url
        self.api_key = s.overseerr_api_key

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key} if self.api_key else {}

    async def _call(self, method: str, path: str, *, body: Any = None,
                    **params: Any) -> Any:
        if not self.base:
            raise UpstreamError(self.service, "no Overseerr URL saved")
        return await request_json(
            self.service, method, f"{self.base}/api/v1{path}",
            headers=self._headers, params=params or None, json_body=body,
        )

    async def ping(self) -> str:
        """Version, from the one endpoint that needs no key at all."""
        data = await self._call("GET", "/status") or {}
        version = data.get("version")
        if not version:
            raise UpstreamError(
                self.service,
                "answered, but not like an Overseerr — check the URL points at "
                "Overseerr itself, not a reverse proxy path",
            )
        return str(version)

    async def users(self) -> list[OverseerrUser]:
        """Everyone Overseerr knows, for attributing requests to the right one."""
        data = await self._call("GET", "/user", take=200) or {}
        out = []
        for item in data.get("results") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            out.append(
                OverseerrUser(
                    user_id=int(item["id"]),
                    name=str(
                        item.get("displayName") or item.get("username")
                        or item.get("plexUsername") or item.get("email") or "unnamed"
                    ),
                    email=item.get("email"),
                )
            )
        return out

    async def media_states(self, take: int = 500) -> dict[int, MediaState]:
        """What Overseerr knows about, keyed by TMDB id.

        One sweep rather than a call per card: a Discover page showing thirty
        suggestions would otherwise be thirty requests to draw one screen.
        """
        data = await self._call("GET", "/request", take=take, sort="added") or {}
        out: dict[int, MediaState] = {}
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            media = item.get("media") or {}
            tmdb_id = media.get("tmdbId")
            if not tmdb_id:
                continue
            # The media status is the useful one: a request can be approved
            # while the show is still downloading, and "approved" is not an
            # answer to "can I watch it".
            status = STATUS.get(int(media.get("status") or 1), "unknown")
            who = (item.get("requestedBy") or {}).get("displayName")
            out[int(tmdb_id)] = MediaState(int(tmdb_id), status, who)
        return out

    async def request_tv(self, tmdb_id: int, *, user_id: int | None = None,
                         seasons: Any = "all") -> str:
        """Ask for a show. Returns the status Overseerr gives back.

        `seasons="all"` is Overseerr's own wording for the whole run, which
        is what pinning something you do not own means.
        """
        if not self.api_key:
            raise UpstreamError(self.service, "no API key saved — requests need one")
        body: dict[str, Any] = {
            "mediaType": "tv",
            "mediaId": int(tmdb_id),
            "seasons": seasons,
        }
        if user_id:
            body["userId"] = int(user_id)
        data = await self._call("POST", "/request", body=body) or {}
        media = data.get("media") or {}
        return STATUS.get(int(media.get("status") or 2), "pending")
