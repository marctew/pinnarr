"""One show: its page, its full guide, and its ratings."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app import watching
from app.clients.http import UpstreamError
from app.clients.overseerr import REQUESTED
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
    media_state,
    next_unwatched,
    owned_tmdb_ids,
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


def _outcome(result) -> JSONResponse:
    return JSONResponse(
        {
            "ok": result.ok,
            "detail": result.detail,
            "marked": result.marked,
            "cleared": result.cleared,
        },
        status_code=200 if result.ok else 409,
    )


@router.post("/api/series/{series_id}/refresh-watched")
async def refresh_watched(request: Request, series_id: int) -> JSONResponse:
    """Re-read this show's watch state from Plex, now.

    The hourly sweep gets there eventually. This is for when you have just
    watched something and want the page to agree with you.
    """
    return _outcome(
        await watching.refresh(int(request.state.user["id"]), series_id)
    )


@router.post("/api/series/{series_id}/watched")
async def mark_series_watched(request: Request, series_id: int) -> JSONResponse:
    """Mark a whole show, or one season, watched or unwatched — in Plex.

    Not locally: Plex is authoritative for anyone with a token, so a mark
    that did not reach it would be cleared by the next sweep. Writing to
    Plex is the only way to make it stick, which is also why it needs your
    own token rather than the server's.
    """
    form = await request.form()
    watched = str(form.get("watched", "true")).lower() not in ("false", "0", "off")
    raw_season = str(form.get("season", "")).strip()
    season = int(raw_season) if raw_season else None

    return _outcome(
        await watching.set_watched(
            int(request.state.user["id"]), series_id,
            watched=watched, season=season,
        )
    )


@router.post("/api/episodes/{episode_id}/watched")
async def mark_episode_watched(request: Request, episode_id: int) -> JSONResponse:
    form = await request.form()
    watched = str(form.get("watched", "true")).lower() not in ("false", "0", "off")

    with session() as conn:
        row = conn.execute(
            "SELECT series_id FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no such episode")

    return _outcome(
        await watching.set_watched(
            int(request.state.user["id"]), int(row["series_id"]),
            watched=watched, episode_id=episode_id,
        )
    )


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

    # The rest of the career, so a forty-credit actor does not read as three
    # shows. Fetched live rather than stored: it is one call, only when
    # somebody opens the page, and it would otherwise be a table nobody reads
    # kept fresh by a job nobody asked for.
    elsewhere: list = []
    if get_settings().tmdb_configured:
        try:
            credits = await TmdbClient().tv_credits(tmdb_person_id)
        except UpstreamError as exc:
            log.warning("no filmography for %s: %s", who["name"], exc)
            credits = []
        mine = {int(r["id"]) for r in roles}
        with session() as conn:
            owned = owned_tmdb_ids(conn, [c["tmdb_id"] for c in credits])
            for credit in credits:
                held = owned.get(credit["tmdb_id"])
                if held and int(held["id"]) in mine:
                    continue  # Already above, with your own watch state on it.
                state = media_state(conn, credit["tmdb_id"])
                elsewhere.append(
                    {
                        **credit,
                        "owned": held,
                        "status": state["status"] if state else None,
                    }
                )

    return templates.TemplateResponse(
        request,
        "person.html",
        {
            "who": who,
            "roles": roles,
            "seen": [r for r in roles if int(r["seen"] or 0) > 0],
            "elsewhere": elsewhere,
            "can_request": get_settings().overseerr_requests_enabled,
            "statuses": REQUESTED,
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
        # A show that only exists here because somebody asked for it has no
        # episodes and no Sonarr entry, and would otherwise read as broken
        # rather than as pending.
        asked = media_state(conn, row["tmdb_id"]) if row["tmdb_id"] else None
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
            "asked": asked,
            "awaiting": asked is not None and not row["in_sonarr"] and not seasons,
            "cast": players,
            "familiar": familiar,
        },
    )
