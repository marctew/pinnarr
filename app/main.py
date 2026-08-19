"""Pinnarr application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.config import get_settings
from app.db import last_runs, migrate
from app.jobs import REGISTRY, build_scheduler

log = logging.getLogger(__name__)


def configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()

    migrate()
    log.info("pinnarr %s starting", __version__)

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
        # wait=False: a sync mid-flight shouldn't hold up shutdown. Anything
        # it half-wrote is picked up by the next run; the jobs are upserts.
        scheduler.shutdown(wait=False)
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
