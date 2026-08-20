"""Pinnarr application entrypoint.

The app object, the startup sequence and the one authentication gate that
sits in front of everything. Routes live in app/routes/ — one module per
area, each exposing a `router` that is included below.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__, auth
from app.config import (
    get_bootstrap,
    get_settings,
)
from app.db import migrate, session
from app.jobs import build_scheduler
from app.routes import (
    accounts,
    admin,
    calendar,
    integrations,
    library,
    lists,
    series,
)
from app.routes import webhook as webhook_routes
from app.scheduling import stop_scheduler
from app.web import STATIC_DIR, templates

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

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

for module in (accounts, webhook_routes, admin, library, calendar, lists, series,
               integrations):
    app.include_router(module.router)


#: Reachable without a session. Everything else needs one.
#: Sonarr cannot log in, so the webhook is deliberately outside the session
#: gate and authenticated by its own shared secret instead.
PUBLIC_PATHS = frozenset({"/login", "/setup", "/healthz", "/hooks/sonarr"})

#: The stylesheet has to load on the login page, which by definition nobody
#: is signed in for. It is a prefix rather than a path because the mount
#: serves whatever is in app/static.
PUBLIC_PREFIXES = ("/static/",)

#: Admin-only prefixes. Syncing rewrites data everyone sees, so it belongs
#: here rather than being reachable by any signed-in account.
#: /api/backup is here because the export carries every API key and
#: password hash in the install. It is the single most sensitive
#: response the app can produce.
ADMIN_PREFIXES = ("/settings", "/api/sync", "/api/backup")


def _presented_key(request: Request) -> str | None:
    """A key from either header. Bearer because that is what most clients
    reach for; X-Api-Key because that is what the *arr stack taught everyone
    to expect, and this sits beside Sonarr."""
    header = request.headers.get("authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-key")


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in (
        request.headers.get("accept") or ""
    )


@app.middleware("http")
async def authenticate(request: Request, call_next):
    """One gate in front of everything, rather than a decorator per route.

    Missing a route is the failure mode that matters here, and an allowlist
    fails closed: a new endpoint is private until someone says otherwise.
    """
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
        return await call_next(request)

    key = _presented_key(request)
    with session() as conn:
        user = auth.user_for_token(conn, request.cookies.get(auth.COOKIE))
        if user is None and key:
            # Integrations have no browser to hold a cookie and no form to
            # post, so they present a key instead. Same account, same role,
            # same per-user data — only the way in differs.
            user = auth.user_for_api_key(conn, key)
        if user is None:
            # Nobody has an account yet: send them to make the first admin
            # rather than to a login form no password can satisfy.
            first_run = auth.admin_count(conn) == 0

    if user is None:
        if key or _wants_json(request):
            # A machine gets an answer it can act on rather than a redirect
            # to a login form it cannot fill in.
            return JSONResponse(
                {"detail": "invalid or missing API key"}, status_code=401
            )
        if first_run:
            return RedirectResponse("/setup", status_code=303)
        nxt = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse(f"/login?next={nxt}", status_code=303)

    request.state.user = user
    if path.startswith(ADMIN_PREFIXES) and user["role"] != auth.ADMIN:
        if _wants_json(request):
            return JSONResponse({"detail": "admin only"}, status_code=403)
        return templates.TemplateResponse(
            request, "forbidden.html", {"needed": "an admin"}, status_code=403
        )
    return await call_next(request)
