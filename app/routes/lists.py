"""The four curated lists: ready to watch, gaps, retire and discover."""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import labels
from app.db import session
from app.episodes import decorate
from app.episodes import parse as parse_dt
from app.repo import (
    COLD_MONTHS,
    READY_DAYS,
    STALLED_HOURS,
    at_risk,
    cold_pins,
    discover_announced,
    discover_counts,
    discover_dated,
    downloads,
    finished_pins,
    gaps,
    latest_retire_batch,
    pinned_count,
    plex_shortfall,
    ready_to_watch,
    retire,
    section_titles,
    suggested,
    undo_retire,
)
from app.web import now_local, templates

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/ready")
async def ready(request: Request, fits: str = ""):
    """What has landed and is waiting, rather than what is coming.

    The calendar is built around anticipation. This is the other half: for
    anyone who watches after a download rather than on transmission, "what
    can I put on now" is the question they actually have.
    """
    user_id = int(request.state.user["id"])
    now, tz = now_local()
    since = (now - timedelta(days=READY_DAYS)).isoformat()

    max_runtime: int | None = None
    with suppress(ValueError):
        max_runtime = int(fits) if fits else None

    with session() as conn:
        # An air date in the future is not "ready", however present the file.
        grouped = ready_to_watch(
            conn, user_id, since, now.isoformat(), max_runtime=max_runtime
        )

    today = now.astimezone(tz).date()

    def dress(episode: Any) -> dict[str, Any]:
        row = decorate(episode, now=now, tz=str(tz))
        # Say which one it is. Claiming an episode "arrived" three days ago
        # when all we know is that it aired then would be a small lie.
        stamp = parse_dt(episode["arrived_at"])
        verb = "arrived"
        if stamp is None:
            stamp = parse_dt(episode["air_date_utc"])
            verb = "aired"
        row["arrived"] = (
            f"{verb} {labels.relative_day(stamp.astimezone(tz).date(), today)}"
            if stamp
            else ""
        )
        return row

    return templates.TemplateResponse(
        request,
        "ready.html",
        {
            # Minutes summed here rather than in the template: Jinja's sum
            # filter cannot cope with a NULL runtime, which plenty have.
            "groups": [
                (
                    series,
                    [dress(e) for e in episodes],
                    sum(int(e["runtime"] or 0) for e in episodes),
                )
                for series, episodes in grouped
            ],
            "days": READY_DAYS,
            "fits": fits,
            "total_minutes": sum(
                int(e["runtime"] or 0) for _, episodes in grouped for e in episodes
            ),
        },
    )


@router.get("/downloads")
async def downloads_page(request: Request):
    """What Sonarr is fetching, and what has stopped moving.

    The queue has been synced every minute since long before there was a page
    for it. All that ever showed was a percentage on whichever calendar row
    you happened to be reading, so a download stuck at 3% was indistinguishable
    from one that had only just started.
    """
    user_id = int(request.state.user["id"])
    with session() as conn:
        rows = downloads(conn, user_id)
        pinned_total = pinned_count(conn, user_id)

    return templates.TemplateResponse(
        request,
        "downloads.html",
        {
            "rows": rows,
            "stalled": sum(1 for r in rows if r["stalled"]),
            "stalled_hours": STALLED_HOURS,
            "pinned_total": pinned_total,
        },
    )


@router.get("/gaps")
async def gaps_page(request: Request):
    """Holes in the shows you follow."""
    user_id = int(request.state.user["id"])
    now, tz = now_local()
    with session() as conn:
        grouped = gaps(conn, user_id)
        shortfall = plex_shortfall(conn, user_id)
        risky = at_risk(conn, user_id)
        pinned_total = pinned_count(conn, user_id)

    return templates.TemplateResponse(
        request,
        "gaps.html",
        {
            "groups": [
                (series, [decorate(e, now=now, tz=str(tz)) for e in episodes])
                for series, episodes in grouped
            ],
            "pinned_total": pinned_total,
            "shortfall": shortfall,
            "risky": risky,
        },
    )


@router.get("/retire")
async def retire_page(request: Request, done: str = ""):
    """Pins that can never produce another episode.

    §10 makes this argument for dormant shows; it applies at least as
    strongly to ones that genuinely ended. A pin that cannot produce another
    episode is not a subscription, it is a souvenir.
    """
    user_id = int(request.state.user["id"])
    with session() as conn:
        candidates = finished_pins(conn, user_id)
        cold = cold_pins(conn, user_id)
        undoable = latest_retire_batch(conn, user_id)
    return templates.TemplateResponse(
        request,
        "retire.html",
        {
            "candidates": candidates,
            "cold": cold,
            "cold_months": COLD_MONTHS,
            "flash": done,
            "can_undo": bool(undoable),
        },
    )


@router.post("/api/series/retire")
async def retire_pins(request: Request) -> JSONResponse:
    """Unpin everything the retire page offered, re-running the query rather
    than trusting a list of ids from the browser."""
    user_id = int(request.state.user["id"])
    with session() as conn:
        ids = [int(r["id"]) for r in finished_pins(conn, user_id)]
        removed, batch = retire(conn, user_id, ids)
        total = pinned_count(conn, user_id)
    return JSONResponse({"retired": removed, "batch": batch, "pinned_total": total})


@router.post("/api/series/retire-undo")
async def undo_retire_pins(request: Request) -> JSONResponse:
    """Put the last retired batch back.

    Retire is the most destructive button in the app — one click, every
    finished pin, no per-item confirmation. It said it was undoable long
    before it was."""
    user_id = int(request.state.user["id"])
    with session() as conn:
        batch = latest_retire_batch(conn, user_id)
        restored = undo_retire(conn, user_id, batch) if batch else 0
        total = pinned_count(conn, user_id)
    return JSONResponse({"restored": restored, "pinned_total": total})


@router.post("/api/series/retire-cold")
async def retire_cold(request: Request) -> JSONResponse:
    """Unpin the shows that have gone cold, re-running the query server-side."""
    user_id = int(request.state.user["id"])
    with session() as conn:
        ids = [int(r["id"]) for r in cold_pins(conn, user_id)]
        removed, batch = retire(conn, user_id, ids)
        total = pinned_count(conn, user_id)
    return JSONResponse({"retired": removed, "batch": batch, "pinned_total": total})


@router.get("/discover")
async def discover(request: Request):
    """Unpinned series with something actually coming.

    A 2000-series library is mostly things you have forgotten about. Every
    pin so far has required you to remember a show exists; this is the view
    that does the remembering.
    """
    user_id = int(request.state.user["id"])
    now, tz = now_local()
    soon = (now + timedelta(days=7)).isoformat()

    with session() as conn:
        this_week = discover_dated(conn, user_id, now=now.isoformat(), until=soon)
        later = discover_dated(conn, user_id, now=soon)
        announced = discover_announced(conn, user_id)
        counts = discover_counts(conn, user_id, now.isoformat())
        suggestions = suggested(conn, user_id)
        sections = section_titles(conn)

    return templates.TemplateResponse(
        request,
        "discover.html",
        {
            "this_week": this_week,
            "later": later,
            "announced": announced,
            "suggestions": suggestions,
            "counts": counts,
            "sections": sections,
            "tz": str(tz),
        },
    )
