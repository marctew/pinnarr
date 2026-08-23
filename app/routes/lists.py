"""The four curated lists: ready to watch, gaps, retire and discover."""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.triggers.date import DateTrigger
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import labels
from app.clients.http import UpstreamError
from app.clients.overseerr import REQUESTED, OverseerrClient
from app.clients.tmdb import TmdbClient
from app.config import get_settings
from app.db import session
from app.episodes import decorate
from app.episodes import parse as parse_dt
from app.jobs import tmdb_sync
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
    note_request,
    pinned_count,
    plex_shortfall,
    ready_to_watch,
    requested,
    retire,
    section_titles,
    suggested,
    undo_retire,
    upsert_requested_series,
    wanted,
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
            "mine": sum(1 for r in rows if r["is_pinned"]),
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


@router.post("/api/request/{tmdb_id}")
async def request_show(request: Request, tmdb_id: int) -> JSONResponse:
    """Ask Overseerr for a show Pinnarr does not have.

    Attributed to whoever pressed the button where Pinnarr knows their
    Overseerr account, because the key is a single admin credential and
    everything asked for with it otherwise arrives under one name.
    """
    if not get_settings().overseerr_requests_enabled:
        return JSONResponse(
            {"ok": False, "detail": "Overseerr needs a URL and an API key for this."},
            status_code=409,
        )

    user = request.state.user
    try:
        status = await OverseerrClient().request_tv(
            tmdb_id, user_id=user["overseerr_user_id"]
        )
    except UpstreamError as exc:
        return JSONResponse(
            {"ok": False, "detail": f"Overseerr said no: {exc}"}, status_code=409
        )

    # A request is a decision, and a decision deserves somewhere to live.
    # Without a row there is nothing to link to, nothing to pin, and no
    # answer to "what did I ask for" beyond a name on a card.
    series_id = None
    try:
        summary = await TmdbClient().show_summary(tmdb_id)
    except UpstreamError as exc:
        # The request itself succeeded. Losing the page for it is a smaller
        # failure than pretending the request did not happen.
        log.warning("no TMDB summary for %s: %s", tmdb_id, exc)
        summary = None

    with session() as conn:
        note_request(conn, tmdb_id, status, user["username"])
        if summary:
            series_id = upsert_requested_series(conn, summary)

    _look_for_it_soon(request)
    return JSONResponse(
        {
            "ok": True,
            "status": status,
            "series_id": series_id,
            "url": f"/series/{series_id}" if series_id else None,
            "detail": f"Requested — {status}.",
        }
    )


#: How long to wait before asking Sonarr what it has. Long enough for
#: Overseerr to have told it and for a batch of requests made in one sitting
#: to collapse into a single sweep.
LOOKUP_DELAY_SECONDS = 45


def _look_for_it_soon(request: Request) -> None:
    """Go and look for a requested show rather than waiting until 03:10.

    Two steps, and the second is the one that matters. Sonarr adds the show,
    but its metadata is TVDB's and its tmdbId is usually empty — so the new
    row arrives with no TMDB id, and everything joining Pinnarr to Overseerr
    keys on exactly that. Without resolving it the card keeps pointing at
    TMDB no matter how long you wait.

    One fixed job id with replace_existing, so asking for six things in a row
    is one sweep rather than six walks of a two thousand series library.
    """
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return
    with suppress(Exception):
        scheduler.add_job(
            tmdb_sync.find_requested,
            DateTrigger(
                run_date=datetime.now(UTC) + timedelta(seconds=LOOKUP_DELAY_SECONDS)
            ),
            id="sonarr_series_after_request",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )


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
        # Only worth showing where a request can actually be made. Without a
        # key it is a list of things you cannot have, which is the advert
        # this was always filtered out to avoid being.
        missing = (
            wanted(conn, user_id)
            if get_settings().overseerr_requests_enabled
            else []
        )
        asked = (
            requested(conn, user_id)
            if get_settings().overseerr_requests_enabled
            else []
        )
        sections = section_titles(conn)

    return templates.TemplateResponse(
        request,
        "discover.html",
        {
            "this_week": this_week,
            "later": later,
            "announced": announced,
            "suggestions": suggestions,
            "wanted": missing,
            "asked": asked,
            "statuses": REQUESTED,
            "counts": counts,
            "sections": sections,
            "tz": str(tz),
        },
    )
