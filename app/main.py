"""Pinnarr application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from calendar import monthrange
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app import __version__, labels
from app.config import (
    SCHEDULING_FIELDS,
    SECRET_FIELDS,
    Settings,
    get_bootstrap,
    get_settings,
    save_settings,
)
from app.db import last_runs, migrate, session
from app.episodes import decorate, episode_state
from app.health import test_service
from app.jobs import REGISTRY, build_scheduler
from app.links import externals
from app.media import poster
from app.repo import (
    PAGE_SIZE,
    PIN_STATES,
    SORTS,
    LibraryFilter,
    bulk_pin,
    count_series,
    facet_counts,
    genres_for,
    get_series,
    latest_bulk_batch,
    matching_ids,
    overdue_episodes,
    pinned_by_outlook,
    pinned_count,
    pinned_episodes,
    query_series,
    section_titles,
    series_episodes,
    set_pinned,
    undo_bulk_pin,
)

#: Shown when Plex has no artwork, or is unreachable. Inline so the grid never
#: depends on a static file or a second request that can also fail.
PLACEHOLDER_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 300">'
    b'<rect width="200" height="300" fill="#2d353e"/>'
    b'<text x="100" y="155" text-anchor="middle" fill="#6b7684"'
    b' font-family="sans-serif" font-size="15">no poster</text></svg>'
)

log = logging.getLogger(__name__)


def configure_logging() -> None:
    # Bootstrap: logging is configured before the database is migrated.
    level = get_bootstrap().log_level
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    log.info("pinnarr %s starting", __version__)

    # Strictly before anything reads settings: they live in this database now,
    # and on a fresh install the table holding them does not exist yet.
    migrate()

    settings = get_settings()
    if missing := settings.missing_config():
        # Boot anyway. A misconfigured integration should show up on the
        # health page, not send the container into a crash loop that hides
        # the actual error behind restart noise.
        log.warning("incomplete configuration:")
        for item in missing:
            log.warning("  - %s", item)

    # Building the scheduler imports the job modules, which is also what runs
    # the @tracked decorators and so populates REGISTRY for the manual
    # trigger below. Nothing else imports them.
    scheduler = build_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    log.info("scheduler started, %d jobs registered", len(scheduler.get_jobs()))

    try:
        yield
    finally:
        # Read it back off app.state rather than closing over the local: a
        # settings save swaps in a new scheduler, and the one built here may
        # already be stopped.
        stop_scheduler(app)
        log.info("pinnarr shutting down")


app = FastAPI(
    title="Pinnarr",
    version=__version__,
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)

#: One lock per job name, so a manual trigger can't race the scheduler or a
#: second impatient click. The cron side gets this from max_instances=1.
_job_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(
    outlook_label=labels.outlook,
    outlook_badge=labels.outlook_badge,
    status_label=labels.sonarr_status,
    relative_day=labels.relative_day,
    OUTLOOK=labels.OUTLOOK,
    SONARR_STATUS=labels.SONARR_STATUS,
)


def stop_scheduler(app: FastAPI) -> None:
    """Stop the current scheduler if there is one and it is running.

    wait=False: a sync mid-flight shouldn't hold up shutdown. Anything it
    half-wrote is picked up by the next run, since the jobs are upserts.
    """
    current = getattr(app.state, "scheduler", None)
    if current is not None and current.running:
        current.shutdown(wait=False)


def restart_scheduler(app: FastAPI) -> None:
    """Rebuild the schedule against the settings as they now are.

    Cron triggers bake in the timezone and expression when the job is added,
    so a saved change is inert until the scheduler is built again.
    """
    stop_scheduler(app)
    scheduler = build_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler


@app.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    """Liveness plus a view of what's configured and when each job last ran."""
    settings = get_settings()
    runs = [
        {
            "job": r["job"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "status": r["status"],
            "detail": (r["detail"] or "")[:200],
        }
        for r in last_runs()
    ]
    degraded = [r for r in runs if r["status"] == "error"]

    scheduler = getattr(request.app.state, "scheduler", None)
    running = bool(scheduler and scheduler.running)
    jobs = [
        {
            "id": j.id,
            "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
        }
        for j in (scheduler.get_jobs() if scheduler else [])
    ]

    return JSONResponse(
        {
            # A stopped scheduler is degraded even with a clean sync_log: it
            # means nothing will ever refresh, which a green light would hide.
            "status": "degraded" if (degraded or not running) else "ok",
            "version": __version__,
            "integrations": {
                "plex": settings.plex_configured,
                "sonarr": settings.sonarr_configured,
                "tautulli": settings.tautulli_configured,
                "tmdb": settings.tmdb_configured,
                "ntfy": settings.ntfy_configured,
            },
            "missing_config": settings.missing_config(),
            "scheduler": {"running": running, "jobs": jobs},
            "last_runs": runs,
        }
    )


@app.post("/api/sync/{job}")
async def trigger_sync(job: str) -> JSONResponse:
    """Run one sync job now and report what it did. SPEC §12.

    Deliberately synchronous: this is the debugging path, and the answer you
    want is what the job actually did, not a 202 that tells you nothing. A
    first full library walk can take a while on a large server.
    """
    if job not in REGISTRY:
        raise HTTPException(
            status_code=404,
            detail=f"unknown job {job!r}; known: {', '.join(sorted(REGISTRY)) or 'none'}",
        )

    lock = _job_locks[job]
    if lock.locked():
        raise HTTPException(status_code=409, detail=f"job {job!r} is already running")

    async with lock:
        detail = await REGISTRY[job]()

    # @tracked swallows exceptions and records the outcome, so sync_log is the
    # authority on whether this actually worked — not the absence of a raise.
    row = next((r for r in last_runs() if r["job"] == job), None)
    return JSONResponse(
        {"job": job, "status": row["status"] if row else "unknown", "detail": detail}
    )


# ── Admin panel ──────────────────────────────────


@app.get("/")
async def index(request: Request, month: str | None = None):
    """The calendar is the landing page (SPEC §13)."""
    return templates.TemplateResponse(request, "calendar.html", _calendar_context(month))


@app.get("/settings")
async def settings_page(request: Request, saved: str = "", error: str = ""):
    """Render the panel. Secrets are never sent to the browser — only whether
    each one is set, so the field can say so without leaking it."""
    s = get_settings()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "s": s,
            "saved": {f: bool(getattr(s, f)) for f in SECRET_FIELDS},
            "flash": error or ("Settings saved." if saved else ""),
            "flash_kind": "bad" if error else "ok",
        },
    )


@app.post("/settings")
async def save_settings_form(request: Request) -> RedirectResponse:
    form = await request.form()

    # Checkboxes post a hidden "false" ahead of the checked "true", so the
    # last value for a key is the real one.
    values: dict[str, Any] = {}
    for key in set(form.keys()):
        if key not in Settings.model_fields:
            continue
        value = str(form.getlist(key)[-1])
        # An untouched password box means "keep what's stored", not "clear it".
        if key in SECRET_FIELDS and not value:
            continue
        values[key] = value

    before = get_settings()
    try:
        after = save_settings(values)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "input"
        message = f"{field}: {first['msg']}"
        return RedirectResponse(f"/settings?error={quote(message)}", status_code=303)

    if any(getattr(before, f) != getattr(after, f) for f in SCHEDULING_FIELDS):
        log.info("scheduling settings changed, rebuilding the schedule")
        restart_scheduler(request.app)

    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/api/settings/test/{service}")
async def test_connection(service: str) -> JSONResponse:
    """Try one integration against the saved settings and report back."""
    return JSONResponse(await test_service(service))


# ── Library (SPEC §11) ───────────────────────────


def _filter_from(request: Request) -> LibraryFilter:
    """Build a LibraryFilter from the querystring.

    Filter state lives in the URL so views are bookmarkable, which also means
    this is the one place that has to be tolerant of hand-edited params.
    """
    q = request.query_params

    def many(key: str) -> tuple[str, ...]:
        raw = ",".join(q.getlist(key))
        return tuple(v for v in (part.strip() for part in raw.split(",")) if v)

    sections: list[int] = []
    for value in many("section"):
        with suppress(ValueError):
            sections.append(int(value))

    page = 1
    with suppress(ValueError):
        page = max(1, int(q.get("page", "1")))

    pinned = q.get("pinned", "all")
    sort = q.get("sort", "recent")
    return LibraryFilter(
        search=q.get("q", "").strip(),
        sections=tuple(sections),
        statuses=many("status"),
        outlooks=many("outlook"),
        genres=many("genre"),
        networks=many("network"),
        pinned=pinned if pinned in PIN_STATES else "all",
        sort=sort if sort in SORTS else "recent",
        page=page,
    )


@app.get("/library")
async def library(request: Request):
    f = _filter_from(request)
    with session() as conn:
        total = count_series(conn, f)
        rows = query_series(conn, f)
        facets = facet_counts(conn, f)
        sections = section_titles(conn)
        pinned_total = pinned_count(conn)
        can_undo = latest_bulk_batch(conn) is not None

    pages = max(1, ceil(total / PAGE_SIZE))
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "f": f,
            "series": rows,
            "facets": facets,
            "sections": sections,
            "total": total,
            "pinned_total": pinned_total,
            "page": min(f.page, pages),
            "pages": pages,
            "can_undo": can_undo,
            "sorts": list(SORTS),
            "querystring": str(request.query_params),
        },
    )


@app.get("/poster/{series_id}")
async def poster_image(series_id: int):
    with session() as conn:
        row = get_series(conn, series_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such series")

    result = await poster(series_id, row["poster_url"] or "")
    if result is None:
        # A placeholder rather than a 404: a broken-image icon in a poster
        # grid looks like the app is broken, not like Plex lacks artwork.
        return Response(PLACEHOLDER_SVG, media_type="image/svg+xml")

    content, content_type = result
    return Response(content, media_type=content_type, headers={"Cache-Control": "max-age=86400"})


@app.post("/api/series/{series_id}/pin")
async def pin_series(series_id: int) -> JSONResponse:
    return _set_pin(series_id, True)


@app.post("/api/series/{series_id}/unpin")
async def unpin_series(series_id: int) -> JSONResponse:
    return _set_pin(series_id, False)


def _set_pin(series_id: int, pinned: bool) -> JSONResponse:
    with session() as conn:
        if get_series(conn, series_id) is None:
            raise HTTPException(status_code=404, detail="no such series")
        set_pinned(conn, series_id, pinned)
        total = pinned_count(conn)
    return JSONResponse({"id": series_id, "pinned": pinned, "pinned_total": total})


@app.post("/api/series/bulk-pin")
async def bulk_pin_filtered(request: Request) -> JSONResponse:
    """Pin everything the filter matches.

    The request carries the filter, not a list of ids, and the server re-runs
    it — so nothing can go stale between rendering the grid and clicking the
    button (SPEC §11).
    """
    with session() as conn:
        ids = matching_ids(conn, _filter_from(request))
        count, batch = bulk_pin(conn, ids)
        total = pinned_count(conn)
    return JSONResponse({"pinned": count, "batch": batch, "pinned_total": total})


@app.post("/api/series/bulk-undo")
async def bulk_undo() -> JSONResponse:
    with session() as conn:
        batch = latest_bulk_batch(conn)
        undone = undo_bulk_pin(conn, batch) if batch else 0
        total = pinned_count(conn)
    return JSONResponse({"undone": undone, "pinned_total": total})


# ── Calendar (SPEC §13) ──────────────────────────

AGENDA_DAYS = 14
#: How far past the agenda to look for a "next up" fallback.
LOOKAHEAD_DAYS = 120
#: How far back "aired, not arrived" looks. Beyond this it stops being news
#: and starts being an audit of the back catalogue.
OVERDUE_DAYS = 30


def _now_local() -> tuple[datetime, ZoneInfo]:
    tz = ZoneInfo(get_settings().tz)
    return datetime.now(UTC), tz


def _calendar_context(month: str | None) -> dict[str, Any]:
    now, tz = _now_local()
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
        rows = pinned_episodes(conn, window_start.isoformat(), window_end.isoformat())
        overdue = overdue_episodes(
            conn,
            (today - timedelta(days=OVERDUE_DAYS)).isoformat(),
            now.isoformat(),
        )
        announced = pinned_by_outlook(conn, ("announced",))
        filming = pinned_by_outlook(conn, ("in_production",))
        dormant = pinned_by_outlook(conn, ("dormant", "cancelled"))
        pinned_total = pinned_count(conn)

    episodes = [decorate(r, now=now, tz=str(tz)) for r in rows]

    agenda: dict[date, list[dict[str, Any]]] = defaultdict(list)
    by_month: dict[date, list[dict[str, Any]]] = defaultdict(list)
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
        "weeks": weeks,
        # What the dots on the grid actually are — without this the month view
        # tells you something is happening and refuses to say what.
        "month_episodes": month_episodes,
        "month_heading": month_heading,
        "month_empty": month_empty,
        "next_up": upcoming[:5],
        "showing_this_month": anchor.year == today.year and anchor.month == today.month,
        "month_label": anchor.strftime("%B %Y"),
        "prev_month": (anchor - timedelta(days=1)).strftime("%Y-%m"),
        "next_month": (last + timedelta(days=1)).strftime("%Y-%m"),
    }


@app.get("/calendar")
async def calendar(request: Request, month: str | None = None):
    return templates.TemplateResponse(request, "calendar.html", _calendar_context(month))


@app.get("/api/calendar")
async def calendar_json(start: str | None = None, end: str | None = None) -> JSONResponse:
    """Pinned episodes in a window, with derived state. SPEC §12."""
    now, tz = _now_local()
    today = now.astimezone(tz).date()
    begin = start or today.isoformat()
    finish = end or (today + timedelta(days=AGENDA_DAYS)).isoformat()

    with session() as conn:
        rows = pinned_episodes(conn, begin, finish)

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


@app.get("/series/{series_id}")
async def series_detail(request: Request, series_id: int):
    now, tz = _now_local()
    with session() as conn:
        row = get_series(conn, series_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such series")
        episodes = series_episodes(conn, series_id)
        genres = genres_for(conn, series_id)

    return templates.TemplateResponse(
        request,
        "series.html",
        {
            "s": row,
            "genres": genres,
            "links": externals(row),
            "episodes": [decorate(e, now=now, tz=str(tz)) for e in episodes],
        },
    )
