"""Sonarr client — owns air dates, grab state and season structure."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.clients.http import UpstreamError, request_json
from app.config import get_settings


@dataclass
class SonarrSeries:
    sonarr_id: int
    tvdb_id: int | None
    tmdb_id: int | None
    imdb_id: str | None
    title: str
    sort_title: str | None
    year: int | None
    status: str | None
    network: str | None
    overview: str | None
    monitored: bool
    next_airing: str | None
    previous_airing: str | None
    latest_season: int | None
    # Defaulted so existing constructions stay valid; Sonarr always sends it.
    title_slug: str | None = None
    seasons: list[int] = field(default_factory=list)


@dataclass
class SonarrEpisode:
    sonarr_episode_id: int
    sonarr_series_id: int
    tvdb_id: int | None
    season: int
    episode: int
    title: str | None
    air_date_utc: str | None
    runtime: int | None
    monitored: bool
    has_file: bool


def _series_from_payload(item: dict[str, Any]) -> SonarrSeries:
    seasons = [
        int(s["seasonNumber"])
        for s in item.get("seasons") or []
        # Season 0 is specials; it never represents "a new season is coming".
        if s.get("seasonNumber") not in (None, 0)
    ]
    return SonarrSeries(
        sonarr_id=int(item["id"]),
        tvdb_id=item.get("tvdbId") or None,
        tmdb_id=item.get("tmdbId") or None,
        imdb_id=item.get("imdbId") or None,
        title=item.get("title") or "(untitled)",
        title_slug=item.get("titleSlug"),
        sort_title=item.get("sortTitle"),
        year=item.get("year") or None,
        status=item.get("status"),
        network=item.get("network"),
        overview=item.get("overview"),
        monitored=bool(item.get("monitored")),
        next_airing=item.get("nextAiring"),
        previous_airing=item.get("previousAiring"),
        latest_season=max(seasons) if seasons else None,
        seasons=sorted(seasons),
    )


class SonarrClient:
    service = "sonarr"

    def __init__(self) -> None:
        s = get_settings()
        self.base = s.sonarr_url
        self.api_key = s.sonarr_api_key

    async def _get(self, path: str, **params: Any) -> Any:
        if not self.base or not self.api_key:
            raise UpstreamError(self.service, "not configured")
        return await request_json(
            self.service,
            "GET",
            f"{self.base}/api/v3{path}",
            headers={"X-Api-Key": self.api_key},
            params=params,
        )

    async def ping(self) -> str:
        data = await self._get("/system/status")
        return str((data or {}).get("version", "unknown"))

    async def series(self) -> list[SonarrSeries]:
        data = await self._get("/series")
        return [_series_from_payload(item) for item in data or []]

    async def calendar(self, start: date, end: date) -> list[SonarrEpisode]:
        """Episodes airing in [start, end).

        Fetched for the whole library in one call rather than per pinned
        series — one request beats fifteen, and pinning becomes instant
        because the data is already local.
        """
        data = await self._get(
            "/calendar",
            start=start.isoformat(),
            end=end.isoformat(),
            unmonitored="true",
            includeSeries="true",
        )
        episodes = []
        for item in data or []:
            series = item.get("series") or {}
            episodes.append(
                SonarrEpisode(
                    sonarr_episode_id=int(item["id"]),
                    sonarr_series_id=int(item.get("seriesId") or 0),
                    tvdb_id=series.get("tvdbId") or None,
                    season=int(item.get("seasonNumber") or 0),
                    episode=int(item.get("episodeNumber") or 0),
                    title=item.get("title"),
                    # airDateUtc is authoritative. The sibling `airDate` field is
                    # network-local and puts US shows on the wrong UK day.
                    air_date_utc=item.get("airDateUtc"),
                    runtime=series.get("runtime"),
                    monitored=bool(item.get("monitored")),
                    has_file=bool(item.get("hasFile")),
                )
            )
        return episodes

    async def episodes_for_series(self, sonarr_series_id: int) -> list[SonarrEpisode]:
        """Full episode list for one series — used to find the latest aired season."""
        data = await self._get("/episode", seriesId=sonarr_series_id)
        return [
            SonarrEpisode(
                sonarr_episode_id=int(item["id"]),
                sonarr_series_id=sonarr_series_id,
                tvdb_id=None,
                season=int(item.get("seasonNumber") or 0),
                episode=int(item.get("episodeNumber") or 0),
                title=item.get("title"),
                air_date_utc=item.get("airDateUtc"),
                runtime=None,
                monitored=bool(item.get("monitored")),
                has_file=bool(item.get("hasFile")),
            )
            for item in data or []
        ]
