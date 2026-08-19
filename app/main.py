"""Pinnarr application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app import __version__
from app.config import (
    SCHEDULING_FIELDS,
    SECRET_FIELDS,
    Settings,
    get_bootstrap,
    get_settings,
    save_settings,
)
from app.db import last_runs, migrate
from app.health import test_service
from app.jobs import REGISTRY, build_scheduler

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
async def index() -> RedirectResponse:
    # The calendar takes this route once it exists; until then the panel is
    # the only thing here, and a 404 on the front page helps nobody.
    return RedirectResponse("/settings", status_code=307)


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
