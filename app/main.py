"""Pinnarr application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import __version__
from app.config import get_settings
from app.db import last_runs, migrate

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

    yield

    log.info("pinnarr shutting down")


app = FastAPI(
    title="Pinnarr",
    version=__version__,
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz() -> JSONResponse:
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

    return JSONResponse(
        {
            "status": "degraded" if degraded else "ok",
            "version": __version__,
            "integrations": {
                "plex": settings.plex_configured,
                "sonarr": settings.sonarr_configured,
                "tautulli": settings.tautulli_configured,
                "tmdb": settings.tmdb_configured,
                "ntfy": settings.ntfy_configured,
            },
            "missing_config": settings.missing_config(),
            "last_runs": runs,
        }
    )
