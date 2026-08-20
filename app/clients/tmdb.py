"""TMDB client — owns production status.

The one thing Sonarr and TVDB cannot tell us: whether a show is *cancelled*
or merely *between seasons*. TVDB collapses both into "ended"/"continuing";
TMDB distinguishes them and exposes in_production.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from app.clients.http import UpstreamError, request_json
from app.config import get_settings

log = logging.getLogger(__name__)

BASE = "https://api.themoviedb.org/3"

#: Values we know how to interpret. TMDB doesn't publish this enum cleanly,
#: so anything unrecognised is passed through and treated as `unknown` by the
#: outlook ladder rather than crashing the sync.
KNOWN_STATUSES = {
    "returning series",
    "planned",
    "in production",
    "ended",
    "canceled",
    "pilot",
}


@dataclass
class TmdbShow:
    tmdb_id: int
    status: str | None
    in_production: bool | None
    number_of_seasons: int | None
    next_episode_air_date: str | None
    last_episode_air_date: str | None


@dataclass
class CastMember:
    tmdb_person_id: int
    name: str
    character: str | None
    profile_path: str | None
    episode_count: int
    billing: int


class TmdbClient:
    service = "tmdb"

    def __init__(self) -> None:
        self.api_key = get_settings().tmdb_api_key

    async def _get(self, path: str, **params: Any) -> Any:
        if not self.api_key:
            raise UpstreamError(self.service, "not configured (TMDB_API_KEY)")
        return await request_json(
            self.service, "GET", f"{BASE}{path}", params={"api_key": self.api_key, **params}
        )

    async def ping(self) -> str:
        """Cheapest call that proves the key works."""
        data = await self._get("/configuration")
        base = (data or {}).get("images", {}).get("secure_base_url")
        return f"key accepted (image base {base})" if base else "key accepted"

    async def find_by_tvdb(self, tvdb_id: int) -> int | None:
        """Resolve a TVDB id to a TMDB id. Returns None if TMDB doesn't know it."""
        data = await self._get(f"/find/{tvdb_id}", external_source="tvdb_id")
        results = (data or {}).get("tv_results") or []
        return int(results[0]["id"]) if results else None

    async def season_ratings(self, tmdb_id: int, season: int) -> dict[int, float]:
        """Episode number → vote average, for one season.

        Ratings are cosmetic, so a season TMDB has never heard of is an empty
        result rather than a problem.
        """
        data = await self._get(f"/tv/{tmdb_id}/season/{season}") or {}
        out: dict[int, float] = {}
        for item in data.get("episodes") or []:
            if not isinstance(item, dict):
                continue
            number = item.get("episode_number")
            score = item.get("vote_average")
            if number is None or not score:
                continue
            with contextlib.suppress(TypeError, ValueError):
                out[int(number)] = float(score)
        return out

    async def recommendations(self, tmdb_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """Shows TMDB thinks resemble this one."""
        data = await self._get(f"/tv/{tmdb_id}/recommendations") or {}
        out = []
        for item in (data.get("results") or [])[:limit]:
            if isinstance(item, dict) and item.get("id"):
                out.append({"tmdb_id": int(item["id"]), "title": item.get("name") or ""})
        return out

    async def credits(self, tmdb_id: int, limit: int = 25) -> list[CastMember]:
        """The cast, ranked by how much of the show they are actually in.

        aggregate_credits rather than credits: for television the latter
        returns whoever was billed on the pilot, and a character who joined
        in season three is missing entirely. This one totals across seasons,
        which is also what makes episode_count meaningful — a one-scene guest
        and the lead should not read the same.
        """
        data = await self._get(f"/tv/{tmdb_id}/aggregate_credits") or {}
        out: list[CastMember] = []
        for item in (data.get("cast") or [])[:limit]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            roles = [r for r in (item.get("roles") or []) if isinstance(r, dict)]
            character = ", ".join(
                str(r.get("character")) for r in roles if r.get("character")
            )
            out.append(
                CastMember(
                    tmdb_person_id=int(item["id"]),
                    name=str(item.get("name") or "").strip() or "unknown",
                    character=character or None,
                    profile_path=item.get("profile_path"),
                    episode_count=int(item.get("total_episode_count") or 0),
                    billing=int(item.get("order") if item.get("order") is not None else 999),
                )
            )
        return out

    async def tv_details(self, tmdb_id: int) -> TmdbShow:
        data = await self._get(f"/tv/{tmdb_id}") or {}

        status = data.get("status")
        if status and status.strip().lower() not in KNOWN_STATUSES:
            # Worth surfacing rather than silently mapping to unknown — if TMDB
            # adds a value we care about, this log line is how we find out.
            log.info("unrecognised TMDB status %r for tmdb_id=%s", status, tmdb_id)

        return TmdbShow(
            tmdb_id=tmdb_id,
            status=status,
            in_production=data.get("in_production"),
            number_of_seasons=data.get("number_of_seasons"),
            next_episode_air_date=(data.get("next_episode_to_air") or {}).get("air_date")
            if data.get("next_episode_to_air")
            else None,
            last_episode_air_date=(data.get("last_episode_to_air") or {}).get("air_date")
            if data.get("last_episode_to_air")
            else None,
        )
