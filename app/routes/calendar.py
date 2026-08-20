"""The calendar: month grid, agenda, live progress and the JSON feed. SPEC §13."""

from __future__ import annotations

import logging
from calendar import monthrange
from collections import defaultdict
from contextlib import suppress
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.clients.http import UpstreamError
from app.clients.sonarr import SonarrClient
from app.config import (
    get_settings,
)
from app.db import session
from app.diagnose import why_missing
from app.episodes import decorate, episode_state
from app.repo import (
    continue_watching,
    overdue_episodes,
    pinned_by_outlook,
    pinned_count,
    pinned_episodes,
)
from app.web import now_local, templates

log = logging.getLogger(__name__)

router = APIRouter()


# ── Calendar (SPEC §13) ──────────────────────────

AGENDA_DAYS = 14
#: How far past the agenda to look for a "next up" fallback.
LOOKAHEAD_DAYS = 120
#: How far back "aired, not arrived" looks. Beyond this it stops being news
#: and starts being an audit of the back catalogue.
OVERDUE_DAYS = 30


#: How many to offer. A strip, not a second library page — past about eight
#: it stops being "what was I watching" and starts being a list to search.
CONTINUE_LIMIT = 8


def _calendar_context(user_id: int, month: str | None) -> dict[str, Any]:
    now, tz = now_local()
    today = now.astimezone(tz).date()

    anchor = today.replace(day=1)
    if month:
        with suppress(ValueError):
            anchor = date.fromisoformat(month + "-01")

    grid_start = anchor - timedelta(days=anchor.weekday())
    last = anchor.replace(day=monthrange(anchor.year, anchor.month)[1])
    grid_end = last + timedelta(days=6 - last.weekday())

    agenda_end = today + timedelta(days=AGENDA_DAYS)
    window_start = min(grid_start, today - timedelta(days=OVERDUE_DAYS))
    # Always reach past the agenda, so an empty fortnight can still say what
    # is actually coming instead of just "nothing".
    window_end = max(grid_end, agenda_end, today + timedelta(days=LOOKAHEAD_DAYS)) + timedelta(days=1)

    with session() as conn:
        rows = pinned_episodes(
            conn,
            user_id,
            window_start.isoformat(),
            window_end.isoformat(),
            include_unmonitored=get_settings().show_unmonitored,
            include_specials=get_settings().show_specials,
            include_watched=not get_settings().hide_watched,
        )
        overdue = overdue_episodes(
            conn,
            user_id,
            (today - timedelta(days=OVERDUE_DAYS)).isoformat(),
            now.isoformat(),
            include_specials=get_settings().show_specials,
        )
        announced = pinned_by_outlook(conn, user_id, ("announced",))
        filming = pinned_by_outlook(conn, user_id, ("in_production",))
        dormant = pinned_by_outlook(conn, user_id, ("dormant", "cancelled"))
        pinned_total = pinned_count(conn, user_id)
        resume = continue_watching(conn, user_id, CONTINUE_LIMIT)

    episodes = [decorate(r, now=now, tz=str(tz)) for r in rows]

    agenda: dict[date, list[dict[str, Any]]] = defaultdict(list)
    by_month: dict[date, list[dict[str, Any]]] = defaultdict(list)
    #: Every day the grid shows, including the greyed-out ends of the
    #: neighbouring months — those cells carry episodes too, and a day you
    #: can see but not open would be the odd one out.
    by_day: dict[date, list[dict[str, Any]]] = defaultdict(list)
    marks: dict[date, int] = defaultdict(int)
    names: dict[date, list[dict[str, Any]]] = defaultdict(list)
    upcoming: list[dict[str, Any]] = []

    for ep in episodes:
        if ep["air_local"] is None:
            continue
        day = ep["air_local"].date()
        marks[day] += 1
        names[day].append(
            {
                "id": ep["series_id"],
                "title": ep["series_title"],
                "code": ep["code"],
                "state": ep["state"],
            }
        )
        if today <= day < agenda_end:
            agenda[day].append(ep)
        elif day >= agenda_end:
            upcoming.append(ep)
        if day.year == anchor.year and day.month == anchor.month:
            by_month[day].append(ep)
        if grid_start <= day <= grid_end:
            by_day[day].append(ep)

    weeks: list[list[dict[str, Any]]] = []
    cursor = grid_start
    while cursor <= grid_end:
        week = []
        for _ in range(7):
            week.append(
                {
                    "date": cursor,
                    "in_month": cursor.month == anchor.month,
                    "is_today": cursor == today,
                    "count": marks.get(cursor, 0),
                    "names": names.get(cursor, []),
                    "key": cursor.isoformat(),
                }
            )
            cursor += timedelta(days=1)
        weeks.append(week)

    # Viewing the current month, the agenda above has already covered
    # everything upcoming — repeating it here just makes the page longer. What
    # it hasn't covered is what already aired, so show that instead.
    this_month = anchor.year == today.year and anchor.month == today.month
    month_episodes = sorted(
        (day, eps) for day, eps in by_month.items() if not this_month or day < today
    )
    month_heading = f"Earlier in {anchor.strftime('%B')}" if this_month else anchor.strftime("%B %Y")
    month_empty = (
        "Nothing of yours has aired yet this month."
        if this_month
        else f"Nothing from your pinned shows in {anchor.strftime('%B %Y')}."
    )

    return {
        "today": today,
        "agenda": sorted(agenda.items()),
        "overdue": [decorate(r, now=now, tz=str(tz)) for r in overdue],
        "announced": announced,
        "filming": filming,
        "dormant": dormant,
        "pinned_total": pinned_total,
        "resume": resume,
        "weeks": weeks,
        # What the dots on the grid actually are — without this the month view
        # tells you something is happening and refuses to say what.
        "month_episodes": month_episodes,
        # Rendered once per day, hidden, and revealed on click. Reusing the
        # same macro as the agenda is the point: a second copy of the row
        # markup in JavaScript is a copy that drifts.
        "day_detail": sorted(by_day.items()),
        "month_heading": month_heading,
        "month_empty": month_empty,
        "next_up": upcoming[:5],
        "showing_this_month": anchor.year == today.year and anchor.month == today.month,
        "month_label": anchor.strftime("%B %Y"),
        "prev_month": (anchor - timedelta(days=1)).strftime("%Y-%m"),
        "next_month": (last + timedelta(days=1)).strftime("%Y-%m"),
    }


@router.get("/")
async def index(request: Request, month: str | None = None):
    """The calendar is the landing page (SPEC §13)."""
    return templates.TemplateResponse(
        request, "calendar.html", _calendar_context(int(request.state.user["id"]), month)
    )


@router.get("/calendar")
async def calendar(request: Request, month: str | None = None):
    return templates.TemplateResponse(request, "calendar.html", _calendar_context(month))


@router.get("/api/calendar/live")
async def calendar_live(request: Request) -> JSONResponse:
    """Current state of everything on the user's calendar window.

    Small enough to poll: the page holds the layout and only swaps the parts
    that can change while you are looking at it.
    """
    user_id = int(request.state.user["id"])
    now, tz = now_local()
    today = now.astimezone(tz).date()
    settings = get_settings()

    with session() as conn:
        rows = pinned_episodes(
            conn,
            user_id,
            (today - timedelta(days=OVERDUE_DAYS)).isoformat(),
            (today + timedelta(days=LOOKAHEAD_DAYS)).isoformat(),
            include_unmonitored=settings.show_unmonitored,
            include_specials=settings.show_specials,
            include_watched=not settings.hide_watched,
        )

    return JSONResponse(
        {
            "episodes": {
                str(r["id"]): {
                    "state": (d := decorate(r, now=now, tz=str(tz)))["state"],
                    "label": d["label"],
                    "mark": d["mark"],
                    "progress": d["progress"],
                }
                for r in rows
            }
        }
    )


@router.get("/api/calendar")
async def calendar_json(
    request: Request, start: str | None = None, end: str | None = None
) -> JSONResponse:
    """Pinned episodes in a window, with derived state. SPEC §12."""
    now, tz = now_local()
    today = now.astimezone(tz).date()
    begin = start or today.isoformat()
    finish = end or (today + timedelta(days=AGENDA_DAYS)).isoformat()

    with session() as conn:
        rows = pinned_episodes(
            conn,
            int(request.state.user["id"]),
            begin,
            finish,
            include_unmonitored=get_settings().show_unmonitored,
            include_specials=get_settings().show_specials,
            include_watched=not get_settings().hide_watched,
        )

    return JSONResponse(
        {
            "start": begin,
            "end": finish,
            "episodes": [
                {
                    "series": r["series_title"],
                    "series_id": r["series_id"],
                    "season": r["season"],
                    "episode": r["episode"],
                    "title": r["title"],
                    "air_date_utc": r["air_date_utc"],
                    "state": episode_state(r, now=now, tz=str(tz)),
                }
                for r in rows
            ],
        }
    )


@router.post("/api/episodes/{episode_id}/why")
async def episode_diagnosis(episode_id: int) -> JSONResponse:
    """Ask Sonarr why this hasn't turned up."""
    return JSONResponse(await why_missing(episode_id))


@router.post("/api/episodes/{episode_id}/search")
async def episode_search(episode_id: int) -> JSONResponse:
    """Ask Sonarr to look again.

    The only place Pinnarr writes to another service, and a deliberate
    exception to SPEC §1. It is still curation rather than management:
    nothing here picks a quality profile or a release, it asks the tool that
    owns downloading to do its job.
    """
    with session() as conn:
        row = conn.execute(
            "SELECT sonarr_episode_id FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no such episode")
    if not row["sonarr_episode_id"]:
        raise HTTPException(status_code=409, detail="Sonarr does not track this episode")
    if not get_settings().sonarr_configured:
        raise HTTPException(status_code=409, detail="Sonarr is not configured")

    try:
        command = await SonarrClient().search_episodes([int(row["sonarr_episode_id"])])
    except UpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return JSONResponse({"ok": True, "command": command, "detail": "Sonarr is searching."})
