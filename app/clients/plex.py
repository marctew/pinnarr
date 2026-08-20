"""Plex client — owns "what do I actually have", external IDs and genres.

Plex serves JSON when asked, so we avoid the XML parsing that most
integrations end up doing.
"""

from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.clients.http import UpstreamError, request_json
from app.config import get_settings

log = logging.getLogger(__name__)

# Modern agent: plex://show/5d9c0... with <Guid id="tvdb://83462"/> children.
# Legacy agents put the id straight in the guid attribute instead:
#   com.plexapp.agents.thetvdb://83462/1/2?lang=en
_LEGACY_GUID = re.compile(
    r"com\.plexapp\.agents\.(?P<agent>thetvdb|themoviedb|imdb)://(?P<id>[a-z0-9]+)", re.I
)
_MODERN_GUID = re.compile(r"(?P<agent>tvdb|tmdb|imdb)://(?P<id>[a-z0-9]+)", re.I)

_AGENT_FIELD = {
    "thetvdb": "tvdb_id",
    "tvdb": "tvdb_id",
    "themoviedb": "tmdb_id",
    "tmdb": "tmdb_id",
    "imdb": "imdb_id",
}


@dataclass
class PlexShow:
    rating_key: str
    section_id: int
    title: str
    sort_title: str | None = None
    year: int | None = None
    summary: str | None = None
    thumb: str | None = None
    tvdb_id: int | None = None
    tmdb_id: int | None = None
    #: The plex://show/... identity, which is what Discover and the
    #: watchlist key on.
    plex_guid: str | None = None
    imdb_id: str | None = None
    genres: list[str] = field(default_factory=list)

    @property
    def has_external_id(self) -> bool:
        return bool(self.tvdb_id or self.tmdb_id or self.imdb_id)


def _extract_ids(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull tvdb/tmdb/imdb ids out of whichever GUID shape this library uses."""
    found: dict[str, Any] = {}

    def absorb(raw: str) -> None:
        m = _LEGACY_GUID.search(raw) or _MODERN_GUID.search(raw)
        if not m:
            return
        field_name = _AGENT_FIELD.get(m.group("agent").lower())
        if not field_name or field_name in found:
            return
        value = m.group("id")
        found[field_name] = value if field_name == "imdb_id" else int(value)

    # Modern: a Guid array alongside a plex:// primary guid.
    for guid in payload.get("Guid") or []:
        if isinstance(guid, dict) and guid.get("id"):
            absorb(str(guid["id"]))

    # Legacy: everything is in the top-level guid string.
    if primary := payload.get("guid"):
        absorb(str(primary))
        if str(primary).startswith("plex://"):
            found["plex_guid"] = str(primary)

    return found


@dataclass
class EpisodeView:
    """What Plex says about one episode: whether it has been watched, and its
    own id, which is what a link into Plex needs."""

    watched: bool
    rating_key: str | None
    #: When Plex last played it, ISO. None if Plex did not say — which it
    #: does not for anything unwatched, and occasionally not for an episode
    #: marked watched by hand rather than played.
    viewed_at: str | None = None


def _epoch_to_iso(value: Any) -> str | None:
    """Plex timestamps are Unix seconds. Stamping "now" instead would date
    every watch to whenever the sync happened to run."""
    if not value:
        return None
    try:
        return datetime.fromtimestamp(int(value), UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _view_state(items: list[Any]) -> dict[tuple[int, int], EpisodeView]:
    state: dict[tuple[int, int], EpisodeView] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        season = item.get("parentIndex")
        episode = item.get("index")
        if season is None or episode is None:
            continue
        with contextlib.suppress(TypeError, ValueError):
            # viewCount counts completed plays. A part-watched episode has a
            # viewOffset and no count, which is right: you have not seen it.
            state[(int(season), int(episode))] = EpisodeView(
                watched=bool(item.get("viewCount")),
                rating_key=str(item["ratingKey"]) if item.get("ratingKey") else None,
                viewed_at=_epoch_to_iso(item.get("lastViewedAt")),
            )
    return state


class PlexClient:
    service = "plex"

    def __init__(self, token: str | None = None) -> None:
        s = get_settings()
        self.base = s.plex_url
        # View state is per Plex account, so callers that care whose it is
        # pass that person's token instead of the server-wide one.
        self.token = token or s.plex_token

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Plex-Token": self.token}

    async def _get(self, path: str, **params: Any) -> Any:
        if not self.base or not self.token:
            raise UpstreamError(self.service, "not configured")
        return await request_json(
            self.service, "GET", f"{self.base}{path}", headers=self._headers, params=params
        )

    async def machine_identifier(self) -> str | None:
        """The server's own id, needed to build a link into the Plex web app."""
        data = await self._get("/identity")
        return (data or {}).get("MediaContainer", {}).get("machineIdentifier")

    async def sections(self) -> list[dict[str, Any]]:
        """Every library section, with the metadata agent behind it.

        The agent matters: a library still on com.plexapp.agents.thetvdb
        needs different GUID parsing to a modern plex:// one, and it is not
        otherwise visible without inspecting a series by hand.
        """
        data = await self._get("/library/sections")
        directories = (data or {}).get("MediaContainer", {}).get("Directory", []) or []
        return [
            {
                "id": int(d["key"]),
                "title": d.get("title") or f"Section {d['key']}",
                "type": d.get("type"),
                "agent": d.get("agent") or "unknown",
            }
            for d in directories
        ]

    async def tv_section_ids(self) -> list[int]:
        """Every library section of type "show". Used when PLEX_TV_SECTIONS is blank."""
        data = await self._get("/library/sections")
        directories = (data or {}).get("MediaContainer", {}).get("Directory", []) or []
        return [int(d["key"]) for d in directories if d.get("type") == "show"]

    async def shows(self, section_id: int) -> list[PlexShow]:
        """All shows in a section, with ids and genres resolved.

        We ask for guids on the bulk listing because newer Plex versions
        include them, which turns N+1 requests into one. Where a show comes
        back without them we fall back to fetching its metadata individually —
        so this works on old and new servers without a version check.
        """
        data = await self._get(
            f"/library/sections/{section_id}/all", type=2, includeGuids=1
        )
        items = (data or {}).get("MediaContainer", {}).get("Metadata", []) or []

        shows: list[PlexShow] = []
        needs_detail: list[PlexShow] = []

        for item in items:
            show = PlexShow(
                rating_key=str(item.get("ratingKey")),
                section_id=section_id,
                title=item.get("title") or "(untitled)",
                sort_title=item.get("titleSort") or item.get("title"),
                year=item.get("year"),
                summary=item.get("summary"),
                thumb=item.get("thumb"),
                genres=[g["tag"] for g in item.get("Genre") or [] if g.get("tag")],
            )
            for key, value in _extract_ids(item).items():
                setattr(show, key, value)

            shows.append(show)
            if not show.has_external_id or not show.genres:
                needs_detail.append(show)

        if needs_detail:
            log.info(
                "section %s: %d/%d shows need a detail fetch for ids/genres",
                section_id, len(needs_detail), len(shows),
            )
        for show in needs_detail:
            try:
                await self._hydrate(show)
            except UpstreamError as exc:
                log.warning("could not hydrate %s: %s", show.title, exc)

        return shows

    async def _hydrate(self, show: PlexShow) -> None:
        """Fill in ids and genres from the per-item metadata endpoint."""
        data = await self._get(f"/library/metadata/{show.rating_key}", includeGuids=1)
        meta = ((data or {}).get("MediaContainer", {}).get("Metadata") or [{}])[0]
        for key, value in _extract_ids(meta).items():
            if getattr(show, key, None) in (None, ""):
                setattr(show, key, value)
        if not show.genres:
            show.genres = [g["tag"] for g in meta.get("Genre") or [] if g.get("tag")]

    async def view_state(self, rating_key: str) -> dict[tuple[int, int], EpisodeView]:
        """Every episode Plex returns for a show, and whether it is watched.

        Both halves matter. Plex holds the truth about watched state and
        Tautulli holds a log of plays, so returning only the watched ones
        would let a stale play record outlive the state it came from.

        An explicit container size, because the default is not guaranteed to
        cover a long-running show and a silently truncated response looks
        exactly like an unwatched season.
        """
        data = await self._get(
            f"/library/metadata/{rating_key}/allLeaves",
            **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 2000},
        )
        items = (data or {}).get("MediaContainer", {}).get("Metadata", []) or []
        return _view_state(items)

    async def section_view_state(
        self, section_id: int, *, page: int = 2000
    ) -> dict[str, dict[tuple[int, int], EpisodeView]]:
        """View state for a whole library section, keyed by series.

        One paged sweep instead of a request per show. At two thousand series
        the per-show route is two thousand round trips for a nightly job, and
        Plex will serve the lot in a handful of pages.
        """
        by_series: dict[str, dict[tuple[int, int], EpisodeView]] = {}
        start = 0
        while True:
            data = await self._get(
                f"/library/sections/{section_id}/all",
                type=4,
                **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": page},
            )
            items = (data or {}).get("MediaContainer", {}).get("Metadata", []) or []
            if not items:
                break
            for item in items:
                if not isinstance(item, dict):
                    continue
                show = item.get("grandparentRatingKey")
                if show is None:
                    continue
                by_series.setdefault(str(show), {}).update(_view_state([item]))
            if len(items) < page:
                break
            start += page
        return by_series

    async def scrobble(self, rating_key: str, *, watched: bool) -> None:
        """Mark something watched or unwatched, for whoever's token this is.

        The fourth place Pinnarr writes to another service (SPEC §1), and the
        first that changes something you can see in Plex itself — so it only
        ever happens because somebody pressed a button.

        A rating key here can be an episode, a season or a whole show; Plex
        applies it to everything underneath. GET rather than POST because
        that is what Plex published, not because it is a read.
        """
        await self._get(
            "/:/scrobble" if watched else "/:/unscrobble",
            identifier="com.plexapp.plugins.library",
            key=rating_key,
        )

    async def episode_keys_present(self, rating_key: str) -> set[tuple[int, int]]:
        """(season, episode) pairs actually present in Plex for one show.

        Used by the hourly availability job to answer "is it really there",
        which is a stronger claim than Sonarr's has_file.
        """
        data = await self._get(f"/library/metadata/{rating_key}/allLeaves")
        items = (data or {}).get("MediaContainer", {}).get("Metadata", []) or []
        present: set[tuple[int, int]] = set()
        for item in items:
            season, episode = item.get("parentIndex"), item.get("index")
            if season is not None and episode is not None:
                present.add((int(season), int(episode)))
        return present

    async def fetch_poster(self, thumb: str) -> tuple[bytes, str]:
        """Fetch poster bytes server-side. Returns (content, content_type).

        Deliberately NOT a URL builder. A tokenised Plex URL in an <img src>
        would put X-Plex-Token into the page source of every library view —
        a long-lived credential granting full read access to the library,
        handed to anything that can see the HTML. Instead Pinnarr proxies
        posters via /poster/{series_id} and the token never leaves the
        container. See routes/media.py.
        """
        if not self.base or not self.token:
            raise UpstreamError(self.service, "not configured")

        import httpx

        from app.clients.http import DEFAULT_TIMEOUT

        url = f"{self.base}{thumb}"
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=self._headers)
        resp.raise_for_status()
        return resp.content, resp.headers.get("Content-Type", "image/jpeg")
