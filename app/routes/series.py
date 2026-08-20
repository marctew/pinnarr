"""One show: its page, its full guide, and its ratings."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.clients.http import UpstreamError
from app.clients.sonarr import SonarrClient
from app.clients.tmdb import TmdbClient
from app.config import (
    get_settings,
)
from app.db import session
from app.episodes import decorate
from app.links import externals, missing_links
from app.repo import (
    appearances,
    cast_for,
    episodes_by_season,
    familiar_faces,
    genres_for,
    get_series,
    latest_season,
    mark_episodes_synced,
    next_unwatched,
    person,
    pin_preferences,
    season_progress,
    set_ratings,
    upsert_episode,
)
from app.web import now_local, templates

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/series/{series_id}/episodes")
async def refresh_episodes(request: Request, series_id: int) -> JSONResponse:
    """Pull the whole episode list for one series from Sonarr.

    The nightly calendar sync only covers -7 to +60 days, which is right for
    a calendar and wrong for a series page. This fills the rest in on demand
    rather than syncing thousands of episodes nobody will look at.
    """
    with session() as conn:
        row = get_series(conn, series_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such series")
        sonarr_id = row["sonarr_id"]

    if not sonarr_id:
        raise HTTPException(status_code=409, detail="Sonarr does not track this series")
    if not get_settings().sonarr_configured:
        raise HTTPException(status_code=409, detail="Sonarr is not configured")

    try:
        episodes = await SonarrClient().episodes_for_series(int(sonarr_id))
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    with session() as conn:
        for episode in episodes:
            upsert_episode(conn, series_id, episode)
        mark_episodes_synced(conn, series_id)

    rated, note = await _pull_ratings(
        series_id, row["tmdb_id"], {e.season for e in episodes}
    )
    return JSONResponse(
        {"id": series_id, "episodes": len(episodes), "rated": rated, "ratings": note}
    )


async def _pull_ratings(
    series_id: int, tmdb_id: int | None, seasons: set[int]
) -> tuple[int, str]:
    """Episode scores from TMDB, alongside the Sonarr refresh.

    Cosmetic, so a failure never fails the refresh that actually matters —
    but it says why rather than returning zero and leaving you to wonder
    whether the feature exists.
    """
    if not get_settings().tmdb_configured:
        return 0, "no ratings: TMDB isn't configured"
    if not tmdb_id:
        return 0, "no ratings: this series has no TMDB id yet — run tmdb_status"
    client = TmdbClient()
    rated = 0
    failed = 0
    for season in sorted(s for s in seasons if s > 0):
        try:
            scores = await client.season_ratings(int(tmdb_id), season)
        except Exception as exc:  # noqa: BLE001 — ratings never break a sync
            log.warning("ratings failed for series %s season %s: %s", series_id, season, exc)
            failed += 1
            continue
        with session() as conn:
            rated += set_ratings(conn, series_id, season, scores)

    if failed:
        return rated, f"{rated} rated, {failed} season(s) TMDB could not answer for"
    if not rated:
        return 0, "no ratings: TMDB has no scores for this series"
    return rated, f"{rated} episode(s) rated"


@router.get("/person/{tmdb_person_id}")
async def person_page(request: Request, tmdb_person_id: int):
    """Everything of yours one person is in.

    Not their filmography — the intersection of it with your shelf, which is
    the only part that answers "where do I know them from".
    """
    viewer = int(request.state.user["id"])
    with session() as conn:
        who = person(conn, tmdb_person_id)
        if who is None:
            raise HTTPException(status_code=404, detail="nobody by that id here")
        roles = appearances(conn, tmdb_person_id, viewer)

    return templates.TemplateResponse(
        request,
        "person.html",
        {
            "who": who,
            "roles": roles,
            "seen": [r for r in roles if int(r["seen"] or 0) > 0],
        },
    )


@router.get("/series/{series_id}")
async def series_detail(request: Request, series_id: int):
    now, tz = now_local()
    with session() as conn:
        row = get_series(conn, series_id, int(request.state.user["id"]))
        if row is None:
            raise HTTPException(status_code=404, detail="no such series")
        viewer = int(request.state.user["id"])
        seasons = episodes_by_season(conn, series_id, viewer)
        progress = season_progress(conn, series_id, viewer)
        up_next = next_unwatched(conn, viewer, series_id)
        genres = genres_for(conn, series_id)
        prefs = pin_preferences(conn, viewer, series_id)
        players = cast_for(conn, series_id, viewer)
        familiar = familiar_faces(conn, series_id, viewer)

    return templates.TemplateResponse(
        request,
        "series.html",
        {
            "s": row,
            "genres": genres,
            "links": externals(row),
            "missing_links": missing_links(row),
            "seasons": [
                (number, [decorate(e, now=now, tz=str(tz)) for e in eps])
                for number, eps in seasons
            ],
            # Open where you left off, not simply the newest season: on a
            # part-watched show the latest season is the least useful one.
            "open_season": (
                int(up_next["season"]) if up_next else latest_season(seasons)
            ),
            "up_next": decorate(up_next, now=now, tz=str(tz)) if up_next else None,
            "progress": progress,
            "prefs": prefs,
            "cast": players,
            "familiar": familiar,
        },
    )
