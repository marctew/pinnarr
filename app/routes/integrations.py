"""Read endpoints for things that are not a browser.

Everything Pinnarr knows was reachable only by rendering a page, which is no
use to a home-automation module, a Stream Deck plugin or a shell script. The
existing /api routes are almost all POST actions — this is the other half.

Shaped for a caller that wants one round trip and a flat answer, not a REST
model of the domain. /api/summary in particular is deliberately everything a
dashboard needs at once: a wall display should not make six requests to draw
one screen.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.config import get_settings
from app.db import last_runs, session
from app.episodes import decorate
from app.links import plex_episode
from app.repo import (
    READY_DAYS,
    STALLED_HOURS,
    at_risk,
    continue_watching,
    downloads,
    gaps,
    overdue_episodes,
    pinned_count,
    pinned_episodes,
    ready_to_watch,
)
from app.web import now_local

log = logging.getLogger(__name__)

router = APIRouter()

#: How far ahead /api/schedule looks unless told otherwise.
DEFAULT_DAYS = 7
MAX_DAYS = 120


def _episode(ep: dict[str, Any]) -> dict[str, Any]:
    """One episode, flat, with no row objects or datetimes left in it."""
    return {
        "episode_id": ep["id"],
        "series_id": ep["series_id"],
        "series": ep["series_title"],
        "season": ep["season"],
        "episode": ep["episode"],
        "code": ep["code"],
        "title": ep["title"],
        "air_date_utc": ep["air_date_utc"],
        "air_local": ep["air_local"].isoformat() if ep["air_local"] else None,
        "ends_local": ep["ends_local"].isoformat() if ep["ends_local"] else None,
        "runtime": ep.get("runtime"),
        "state": ep["state"],
        "label": ep["label"],
        "watched": ep["watched"],
        "milestone": ep["milestone"] or None,
        "plex_url": plex_episode(ep.get("plex_rating_key")),
        "download_percent": ep.get("progress"),
    }


@router.get("/api/schedule")
async def schedule(request: Request, days: int = DEFAULT_DAYS, back: int = 1):
    """What is on, for this account's pins.

    `back` exists because "did last night's episode arrive" is a question a
    dashboard asks at breakfast, and a feed that starts at midnight tonight
    cannot answer it.
    """
    user_id = int(request.state.user["id"])
    days = max(1, min(int(days), MAX_DAYS))
    back = max(0, min(int(back), MAX_DAYS))
    settings = get_settings()
    now, tz = now_local()
    start = now - timedelta(days=back)
    end = now + timedelta(days=days)

    with session() as conn:
        rows = pinned_episodes(
            conn, user_id, start.isoformat(), end.isoformat(),
            include_unmonitored=settings.show_unmonitored,
            include_specials=settings.show_specials,
        )
    episodes = [_episode(decorate(r, now=now, tz=str(tz))) for r in rows]
    return JSONResponse(
        {"from": start.isoformat(), "to": end.isoformat(), "count": len(episodes),
         "episodes": episodes}
    )


@router.get("/api/next")
async def next_up(request: Request):
    """The single next thing, which is what a one-line display wants."""
    user_id = int(request.state.user["id"])
    now, tz = now_local()
    with session() as conn:
        rows = pinned_episodes(
            conn, user_id, now.isoformat(),
            (now + timedelta(days=MAX_DAYS)).isoformat(),
        )
    episodes = [decorate(r, now=now, tz=str(tz)) for r in rows]
    upcoming = [e for e in episodes if e["air_local"] and e["air_local"] >= now]
    return JSONResponse({"episode": _episode(upcoming[0]) if upcoming else None})


@router.get("/api/watching")
async def watching(request: Request, limit: int = 8):
    """Shows you are partway through, and the next episode of each."""
    user_id = int(request.state.user["id"])
    with session() as conn:
        rows = continue_watching(conn, user_id, max(1, min(int(limit), 50)))
    return JSONResponse(
        {
            "shows": [
                {
                    "series_id": r["series_id"],
                    "series": r["title"],
                    "season": r["season"],
                    "episode": r["episode"],
                    "code": f"S{r['season']:02d}E{r['episode']:02d}",
                    "title": r["episode_title"],
                    "runtime": r["runtime"],
                    "watched": r["seen"],
                    "owned": r["owned"],
                    "last_watched_at": r["last_seen"],
                    "plex_url": plex_episode(r["plex_rating_key"]),
                }
                for r in rows
            ]
        }
    )


@router.get("/api/downloads")
async def downloading(request: Request):
    """The Sonarr queue for this account's pins, and what has stopped moving."""
    user_id = int(request.state.user["id"])
    with session() as conn:
        rows = downloads(conn, user_id)
    return JSONResponse(
        {
            "stalled_after_hours": STALLED_HOURS,
            "stalled": sum(1 for r in rows if r["stalled"]),
            "items": [
                {
                    "series_id": r["series_id"],
                    "series": r["series_title"],
                    "code": f"S{r['season']:02d}E{r['episode']:02d}",
                    "title": r["episode_title"],
                    "percent": r["percent"],
                    "status": r["status"],
                    "time_left": r["time_left"],
                    "message": r["message"],
                    "stalled": bool(r["stalled"]),
                    "since": r["progress_at"],
                }
                for r in rows
            ],
        }
    )


@router.get("/api/arrivals")
async def arrivals(request: Request, hours: int = 24):
    """What has landed recently — the trigger an automation actually wants.

    Ordered newest first and stamped, so a caller can poll this and act on
    anything newer than the last one it saw.
    """
    user_id = int(request.state.user["id"])
    hours = max(1, min(int(hours), 24 * 30))
    now, tz = now_local()
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    with session() as conn:
        grouped = ready_to_watch(conn, user_id, cutoff.isoformat(), now.isoformat())

    out = []
    for _series, rows in grouped:
        for row in rows:
            item = _episode(decorate(row, now=now, tz=str(tz)))
            item["arrived_at"] = row["arrived_at"]
            out.append(item)
    out.sort(key=lambda e: e["arrived_at"] or "", reverse=True)
    return JSONResponse({"since_hours": hours, "count": len(out), "episodes": out})


@router.get("/api/summary")
async def summary(request: Request):
    """Everything a dashboard needs, in one call.

    Deliberately not RESTful. A wall display redrawing every minute should
    not make six requests to fill one screen, and every number here comes
    from the same instant.
    """
    user_id = int(request.state.user["id"])
    now, tz = now_local()
    today = now.astimezone(tz).date()

    with session() as conn:
        pinned = pinned_count(conn, user_id)
        soon = pinned_episodes(
            conn, user_id, now.isoformat(),
            (now + timedelta(days=MAX_DAYS)).isoformat(),
        )
        late = overdue_episodes(
            conn, user_id, (now - timedelta(days=30)).isoformat(), now.isoformat()
        )
        queue = downloads(conn, user_id)
        unwatched = [
            row
            for _series, rows in ready_to_watch(
                conn, user_id,
                (now - timedelta(days=READY_DAYS)).isoformat(), now.isoformat(),
            )
            for row in rows
        ]
        holes = gaps(conn, user_id)
        risky = at_risk(conn, user_id)
        resume = continue_watching(conn, user_id, 1)

    episodes = [decorate(r, now=now, tz=str(tz)) for r in soon]
    upcoming = [e for e in episodes if e["air_local"] and e["air_local"] >= now]
    tonight = [
        e for e in episodes if e["air_local"] and e["air_local"].date() == today
    ]
    minutes = sum(int(r["runtime"] or 0) for r in unwatched)

    degraded = [r["job"] for r in last_runs() if r["status"] == "error"]

    return JSONResponse(
        {
            "version": __version__,
            "generated_at": datetime.now(UTC).isoformat(),
            "user": request.state.user["username"],
            "pinned": pinned,
            "tonight": [_episode(e) for e in tonight],
            "next": _episode(upcoming[0]) if upcoming else None,
            "counts": {
                "airing_today": len(tonight),
                "upcoming": len(upcoming),
                "overdue": len(late),
                "downloading": len(queue),
                "stalled": sum(1 for r in queue if r["stalled"]),
                "ready_to_watch": len(unwatched),
                "unwatched_minutes": minutes,
                "gaps": sum(len(eps) for _series, eps in holes),
                "at_risk": len(risky),
            },
            "continue_watching": (
                {
                    "series_id": resume[0]["series_id"],
                    "series": resume[0]["title"],
                    "code": f"S{resume[0]['season']:02d}E{resume[0]['episode']:02d}",
                    "plex_url": plex_episode(resume[0]["plex_rating_key"]),
                }
                if resume
                else None
            ),
            # Not "is Pinnarr up" — the caller reached it. Whether what it is
            # reporting is fresh, which is the part a dashboard should show.
            "healthy": not degraded,
            "failing_jobs": degraded,
        }
    )
