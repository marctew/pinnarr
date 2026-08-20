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
    # Defaulted so existing constructions stay valid; Sonarr always sends them.
    title_slug: str | None = None
    poster_url: str | None = None
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
    #: Sonarr v4 labels these itself: "season" or "series".
    finale_type: str | None = None


def _poster_from(item: dict[str, Any]) -> str | None:
    """The absolute poster URL Sonarr knows about, if any.

    remoteUrl is preferred over Sonarr's own /MediaCover path because it
    needs no API key and keeps working when Sonarr is down.
    """
    for image in item.get("images") or []:
        if not isinstance(image, dict):
            continue
        if (image.get("coverType") or "").lower() != "poster":
            continue
        remote = image.get("remoteUrl")
        if remote and str(remote).startswith(("http://", "https://")):
            return str(remote)
    return None


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
        poster_url=_poster_from(item),
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
                    finale_type=item.get("finaleType"),
                )
            )
        return episodes

    async def queue(self) -> list[QueueItem]:
        """What Sonarr is downloading right now.

        This is what separates "arriving" from "stuck": without it, an
        episode that aired and has not appeared looks identical whether a
        download is 80% done or nothing has been found at all.
        """
        data = await self._get("/queue", pageSize=500, includeEpisode="true")
        records = (data or {}).get("records", data) or []
        items = []
        for item in records:
            if not isinstance(item, dict) or not item.get("episodeId"):
                continue
            items.append(
                QueueItem(
                    sonarr_episode_id=int(item["episodeId"]),
                    sonarr_series_id=int(item.get("seriesId") or 0),
                    status=str(item.get("trackedDownloadState") or item.get("status") or ""),
                    percent=_percent(item),
                    time_left=item.get("timeleft"),
                    message=item.get("errorMessage") or None,
                )
            )
        return items

    async def history_for_episode(self, sonarr_episode_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """Recent grab/import/failure events for one episode."""
        data = await self._get(
            "/history", episodeId=sonarr_episode_id, pageSize=limit,
            sortKey="date", sortDirection="descending",
        )
        records = (data or {}).get("records", data) or []
        return [r for r in records if isinstance(r, dict)]

    async def search_episodes(self, episode_ids: list[int]) -> int:
        """Ask Sonarr to go looking. Returns the command id.

        The one place Pinnarr writes to another service — see SPEC §1.
        """
        if not self.base or not self.api_key:
            raise UpstreamError(self.service, "not configured")
        payload = await request_json(
            self.service,
            "POST",
            f"{self.base}/api/v3/command",
            headers={"X-Api-Key": self.api_key},
            json_body={"name": "EpisodeSearch", "episodeIds": episode_ids},
        )
        return int((payload or {}).get("id") or 0)

    async def tags(self) -> dict[str, int]:
        """Every tag Sonarr knows, label → id. Labels are lowercased by Sonarr."""
        data = await self._get("/tag")
        return {
            str(t["label"]).lower(): int(t["id"])
            for t in data or []
            if isinstance(t, dict) and t.get("label") is not None
        }

    async def create_tag(self, label: str) -> int:
        if not self.base or not self.api_key:
            raise UpstreamError(self.service, "not configured")
        data = await request_json(
            self.service, "POST", f"{self.base}/api/v3/tag",
            headers={"X-Api-Key": self.api_key}, json_body={"label": label.lower()},
        )
        return int((data or {}).get("id") or 0)

    async def series_tag_map(self) -> dict[int, set[int]]:
        """Sonarr series id → the tag ids on it.

        Fetched once per sync rather than once per user: the response is the
        entire library, and multiplying that by the number of accounts every
        few minutes would be rude to a service on the same box.
        """
        data = await self._get("/series")
        return {
            int(item["id"]): {int(t) for t in item.get("tags") or []}
            for item in data or []
            if isinstance(item, dict) and item.get("id") is not None
        }

    async def apply_tag(self, series_ids: list[int], tag_id: int, *, add: bool) -> None:
        """Add or remove one tag across many series in a single call.

        The editor endpoint exists for exactly this; the alternative is a
        read-modify-write of each full series resource, which is a race
        waiting to happen.
        """
        if not series_ids:
            return
        if not self.base or not self.api_key:
            raise UpstreamError(self.service, "not configured")
        await request_json(
            self.service, "PUT", f"{self.base}/api/v3/series/editor",
            headers={"X-Api-Key": self.api_key},
            json_body={
                "seriesIds": series_ids,
                "tags": [tag_id],
                "applyTags": "add" if add else "remove",
            },
        )

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
                finale_type=item.get("finaleType"),
            )
            for item in data or []
        ]


@dataclass
class QueueItem:
    sonarr_episode_id: int
    sonarr_series_id: int
    status: str
    percent: float
    time_left: str | None
    message: str | None


def _percent(item: dict[str, Any]) -> float:
    size = float(item.get("size") or 0)
    left = float(item.get("sizeleft") or 0)
    if size <= 0:
        return 0.0
    return max(0.0, min(100.0, (size - left) / size * 100.0))
