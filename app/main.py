"""Pinnarr application entrypoint."""

from __future__ import annotations

import asyncio
import json
import logging
from calendar import monthrange
from collections import defaultdict
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hmac import compare_digest
from math import ceil
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app import __version__, auth, backup, labels
from app import webhook as hooks
from app.clients import watchlist as watchlist_client
from app.clients.http import UpstreamError
from app.clients.sonarr import SonarrClient
from app.clients.tmdb import TmdbClient
from app.config import (
    SCHEDULING_FIELDS,
    SECRET_FIELDS,
    Settings,
    get_bootstrap,
    get_settings,
    save_settings,
)
from app.db import last_runs, migrate, session, utcnow
from app.diagnose import why_missing
from app.episodes import decorate, episode_state
from app.episodes import parse as parse_dt
from app.health import test_service
from app.jobs import REGISTRY, build_scheduler
from app.links import externals, missing_links
from app.media import poster
from app.repo import (
    PAGE_SIZE,
    PIN_STATES,
    READY_DAYS,
    SORTS,
    LibraryFilter,
    adopt_orphaned_pins,
    bulk_pin,
    count_series,
    discover_announced,
    discover_counts,
    discover_dated,
    episodes_by_season,
    facet_counts,
    finished_pins,
    gaps,
    genres_for,
    get_series,
    is_pinned_by,
    latest_bulk_batch,
    latest_season,
    mark_episodes_synced,
    matching_ids,
    overdue_episodes,
    pinned_by_outlook,
    pinned_count,
    pinned_episodes,
    plex_shortfall,
    query_series,
    ready_to_watch,
    retire,
    season_progress,
    section_titles,
    set_notify,
    set_pinned,
    set_ratings,
    suggested,
    undo_bulk_pin,
    upsert_episode,
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
#: Reachable without a session. Everything else needs one.
#: Sonarr cannot log in, so the webhook is deliberately outside the session
#: gate and authenticated by its own shared secret instead.
PUBLIC_PATHS = frozenset({"/login", "/setup", "/healthz", "/hooks/sonarr"})

#: Admin-only prefixes. Syncing rewrites data everyone sees, so it belongs
#: here rather than being reachable by any signed-in account.
#: /api/backup is here because the export carries every API key and
#: password hash in the install. It is the single most sensitive
#: response the app can produce.
ADMIN_PREFIXES = ("/settings", "/api/sync", "/api/backup")


def current_user(request: Request):
    return getattr(request.state, "user", None)


templates.env.globals.update(
    current_user=current_user,
    outlook_label=labels.outlook,
    outlook_badge=labels.outlook_badge,
    status_label=labels.sonarr_status,
    relative_day=labels.relative_day,
    duration=labels.duration,
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


@app.middleware("http")
async def authenticate(request: Request, call_next):
    """One gate in front of everything, rather than a decorator per route.

    Missing a route is the failure mode that matters here, and an allowlist
    fails closed: a new endpoint is private until someone says otherwise.
    """
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)

    with session() as conn:
        user = auth.user_for_token(conn, request.cookies.get(auth.COOKIE))
        if user is None:
            # Nobody has an account yet: send them to make the first admin
            # rather than to a login form no password can satisfy.
            first_run = auth.admin_count(conn) == 0

    if user is None:
        if first_run:
            return RedirectResponse("/setup", status_code=303)
        nxt = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse(f"/login?next={nxt}", status_code=303)

    request.state.user = user
    if path.startswith(ADMIN_PREFIXES) and user["role"] != auth.ADMIN:
        return templates.TemplateResponse(
            request, "forbidden.html", {"needed": "an admin"}, status_code=403
        )
    return await call_next(request)


# ── Sign in ──────────────────────────────────────


@app.get("/setup")
async def setup_form(request: Request, error: str = ""):
    if auth.user_count():
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"error": error})


@app.post("/setup")
async def setup_submit(request: Request):
    """Create the first admin. Only ever available while there are no users."""
    if auth.user_count():
        return RedirectResponse("/login", status_code=303)

    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    if not username or len(password) < 8:
        return RedirectResponse(
            "/setup?error=" + quote("A username and a password of at least 8 characters."),
            status_code=303,
        )

    with session() as conn:
        user_id = auth.create_user(conn, username, password, auth.ADMIN)
        adopted = adopt_orphaned_pins(conn, user_id)
        token = auth.start_session(conn, user_id)
    if adopted:
        log.info("adopted %d pre-existing pins for the first admin", adopted)

    response = RedirectResponse("/", status_code=303)
    _set_cookie(response, token)
    return response


@app.get("/login")
async def login_form(request: Request, error: str = "", next: str = "/"):
    if not auth.user_count():
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": error, "next": next})


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    nxt = str(form.get("next") or "/")
    user = auth.authenticate(str(form.get("username", "")), str(form.get("password", "")))
    if user is None:
        return RedirectResponse(
            f"/login?error={quote('Wrong username or password.')}&next={quote(nxt)}",
            status_code=303,
        )

    with session() as conn:
        token = auth.start_session(conn, int(user["id"]))
    # Only ever redirect within the app: an open redirect on a login form is
    # a phishing primitive.
    response = RedirectResponse(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/",
                                status_code=303)
    _set_cookie(response, token)
    return response


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(auth.COOKIE)
    if token:
        with session() as conn:
            auth.end_session(conn, token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE, path="/")
    return response


def _set_cookie(response, token: str) -> None:
    response.set_cookie(
        auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400,
        httponly=True, samesite="lax", path="/",
    )


# ── Your own account ─────────────────────────────


@app.get("/profile")
async def profile(request: Request, saved: str = "", error: str = ""):
    return templates.TemplateResponse(
        request, "profile.html",
        {"flash": error or ("Saved." if saved else ""), "flash_kind": "bad" if error else "ok"},
    )


@app.post("/profile")
async def profile_save(request: Request):
    form = await request.form()
    user = request.state.user
    topic = str(form.get("ntfy_topic", "")).strip()
    password = str(form.get("password", ""))
    plex_token = str(form.get("plex_token", ""))

    if password and len(password) < 8:
        return RedirectResponse(
            "/profile?error=" + quote("Password must be at least 8 characters."), status_code=303
        )

    with session() as conn:
        auth.set_topic(conn, int(user["id"]), topic)
        # An empty box means keep, as everywhere else a secret is edited.
        if plex_token.strip():
            auth.set_plex_token(conn, int(user["id"]), plex_token)
        if password:
            auth.set_password(conn, int(user["id"]), password)

    if password:
        # set_password drops every session, including this one.
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/profile?saved=1", status_code=303)


# ── Accounts (admin) ─────────────────────────────


@app.get("/settings/users")
async def users_page(request: Request, saved: str = "", error: str = ""):
    with session() as conn:
        users = auth.list_users(conn)
    return templates.TemplateResponse(
        request, "users.html",
        {
            "users": users,
            "flash": error or ("Saved." if saved else ""),
            "flash_kind": "bad" if error else "ok",
        },
    )


@app.post("/settings/users")
async def users_action(request: Request):
    form = await request.form()
    action = str(form.get("action", ""))
    me = int(request.state.user["id"])

    def back(message: str = "", ok: bool = True):
        if not message:
            return RedirectResponse("/settings/users?saved=1", status_code=303)
        key = "saved" if ok else "error"
        return RedirectResponse(f"/settings/users?{key}={quote(message)}", status_code=303)

    with session() as conn:
        if action == "create":
            username = str(form.get("username", "")).strip()
            password = str(form.get("password", ""))
            if not username or len(password) < 8:
                return back("A username and a password of at least 8 characters.", ok=False)
            if auth.by_username(conn, username):
                return back(f"{username} already exists.", ok=False)
            auth.create_user(conn, username, password, str(form.get("role", auth.USER)))
            return back()

        target = int(form.get("user_id", 0) or 0)
        if target == 0 or auth.get_user(conn, target) is None:
            return back("No such user.", ok=False)

        if action == "delete":
            if target == me:
                return back("You cannot delete your own account.", ok=False)
            if auth.get_user(conn, target)["role"] == auth.ADMIN and auth.admin_count(conn) <= 1:
                return back("That is the only admin left.", ok=False)
            auth.delete_user(conn, target)
        elif action == "password":
            password = str(form.get("password", ""))
            if len(password) < 8:
                return back("Password must be at least 8 characters.", ok=False)
            auth.set_password(conn, target, password)
        elif action == "role":
            role = str(form.get("role", auth.USER))
            # Demoting the last admin locks everyone out of configuration.
            if (
                role != auth.ADMIN
                and auth.get_user(conn, target)["role"] == auth.ADMIN
                and auth.admin_count(conn) <= 1
            ):
                return back("That is the only admin left.", ok=False)
            auth.set_role(conn, target, role)
    return back()


@app.post("/hooks/sonarr")
async def sonarr_webhook(request: Request) -> JSONResponse:
    """Sonarr's On Import / On Upgrade connection.

    Always answers 200 once the secret checks out. Sonarr disables a
    connection that keeps failing, so a parser problem must not be reported
    as an HTTP error — it is recorded and shown in the panel instead.
    """
    secret = get_settings().webhook_secret
    if not secret:
        raise HTTPException(status_code=503, detail="no webhook secret configured")
    if not compare_digest(request.query_params.get("secret", ""), secret):
        log.warning("webhook rejected: bad secret from %s", request.client.host if request.client else "?")
        raise HTTPException(status_code=403, detail="bad secret")

    raw = await request.body()
    payload = hooks.payload_from(raw)
    text = raw.decode("utf-8", "replace")

    if payload is None:
        hooks.record("unparseable", False, "body was not JSON", text)
        return JSONResponse({"ok": False, "detail": "body was not JSON"})

    try:
        detail = await hooks.handle(payload, text)
    except Exception as exc:  # noqa: BLE001 — never hand Sonarr a 500
        log.exception("webhook handler failed")
        hooks.record("error", False, f"{type(exc).__name__}: {exc}", text)
        return JSONResponse({"ok": False, "detail": "handler error, logged"})

    return JSONResponse({"ok": True, "detail": detail})


@app.get("/settings/webhook")
async def webhook_page(request: Request):
    settings = get_settings()
    base = settings.pinnarr_base_url.rstrip("/")
    url = (
        f"{base}/hooks/sonarr?secret={settings.webhook_secret}"
        if settings.webhook_secret
        else None
    )
    return templates.TemplateResponse(
        request, "webhook.html", {"url": url, "deliveries": hooks.recent()}
    )


@app.post("/api/series/{series_id}/notify")
async def series_notify(request: Request, series_id: int) -> JSONResponse:
    """Per-series notification opt-out, per user. SPEC §12."""
    form = await request.form()
    wanted = str(form.get("notify", "true")).lower() not in ("false", "0", "off")
    user_id = int(request.state.user["id"])

    with session() as conn:
        if not is_pinned_by(conn, user_id, series_id):
            raise HTTPException(status_code=404, detail="you have not pinned that series")
        set_notify(conn, user_id, series_id, wanted)
    return JSONResponse({"id": series_id, "notify": wanted})


@app.post("/api/profile/watchlist-test")
async def watchlist_test(request: Request) -> JSONResponse:
    """Check the signed-in user's own Plex token against their watchlist."""
    with session() as conn:
        row = auth.get_user(conn, int(request.state.user["id"]))
    return JSONResponse(await watchlist_client.check(row["plex_token"] or ""))


@app.get("/settings/backup")
async def backup_page(request: Request, restored: str = "", error: str = ""):
    return templates.TemplateResponse(
        request, "backup.html",
        {"flash": error or restored, "flash_kind": "bad" if error else "ok"},
    )


@app.get("/api/backup")
async def backup_download() -> Response:
    """The three things that cannot be rebuilt from Plex and Sonarr.

    Contains secrets and password hashes by design — a backup that omits
    them is one that fails at the worst possible moment.
    """
    payload = json.dumps(backup.export(), indent=2)
    stamp = utcnow()[:10]
    return Response(
        payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="pinnarr-backup-{stamp}.json"'},
    )


@app.post("/settings/backup")
async def backup_restore(request: Request) -> RedirectResponse:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return RedirectResponse(
            "/settings/backup?error=" + quote("Choose a backup file first."), status_code=303
        )

    try:
        payload = json.loads(await upload.read())
        report = backup.restore(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        return RedirectResponse(
            f"/settings/backup?error={quote(f'Could not read that file: {exc}')}",
            status_code=303,
        )

    message = (
        f"Restored {report['users']} account(s), {report['pins']} pin(s) "
        f"and {report['settings']} setting(s)."
    )
    if report["unmatched"]:
        shown = ", ".join(report["unmatched"][:5])
        more = "" if len(report["unmatched"]) <= 5 else f" and {len(report['unmatched']) - 5} more"
        message += f" Not in this library yet: {shown}{more} — run the syncs and restore again."
    return RedirectResponse(f"/settings/backup?restored={quote(message)}", status_code=303)


@app.get("/settings/jobs")
async def jobs_page(request: Request):
    """Every sync job with its last result and a way to run it now.

    The manual trigger existed from the start, but only as a curl against
    /api/sync — which stopped working the moment the app grew a login.
    """
    scheduler = getattr(request.app.state, "scheduler", None)
    next_runs = {
        j.id: (j.next_run_time.isoformat() if j.next_run_time else None)
        for j in (scheduler.get_jobs() if scheduler else [])
    }
    last = {r["job"]: r for r in last_runs()}

    jobs = [
        {
            "name": name,
            "last": last.get(name),
            "next_run": next_runs.get(name),
        }
        for name in sorted(REGISTRY)
    ]
    return templates.TemplateResponse(request, "jobs.html", {"jobs": jobs})


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
    return templates.TemplateResponse(
        request, "calendar.html", _calendar_context(int(request.state.user["id"]), month)
    )


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
    f = replace(_filter_from(request), user_id=int(request.state.user["id"]))
    with session() as conn:
        total = count_series(conn, f)
        rows = query_series(conn, f)
        facets = facet_counts(conn, f)
        sections = section_titles(conn)
        pinned_total = pinned_count(conn, f.user_id)
        can_undo = latest_bulk_batch(conn, f.user_id) is not None

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

    result = await poster(
        series_id,
        plex_thumb=row["poster_url"] or "",
        remote_url=row["remote_poster"] or "",
    )
    if result is None:
        # A placeholder rather than a 404: a broken-image icon in a poster
        # grid looks like the app is broken, not like Plex lacks artwork.
        return Response(PLACEHOLDER_SVG, media_type="image/svg+xml")

    content, content_type = result
    return Response(content, media_type=content_type, headers={"Cache-Control": "max-age=86400"})


@app.post("/api/series/{series_id}/pin")
async def pin_series(request: Request, series_id: int) -> JSONResponse:
    return _set_pin(int(request.state.user["id"]), series_id, True)


@app.post("/api/series/{series_id}/unpin")
async def unpin_series(request: Request, series_id: int) -> JSONResponse:
    return _set_pin(int(request.state.user["id"]), series_id, False)


def _set_pin(user_id: int, series_id: int, pinned: bool) -> JSONResponse:
    with session() as conn:
        if get_series(conn, series_id) is None:
            raise HTTPException(status_code=404, detail="no such series")
        set_pinned(conn, user_id, series_id, pinned)
        total = pinned_count(conn, user_id)
    return JSONResponse({"id": series_id, "pinned": pinned, "pinned_total": total})


@app.post("/api/series/bulk-pin")
async def bulk_pin_filtered(request: Request) -> JSONResponse:
    """Pin everything the filter matches.

    The request carries the filter, not a list of ids, and the server re-runs
    it — so nothing can go stale between rendering the grid and clicking the
    button (SPEC §11).
    """
    user_id = int(request.state.user["id"])
    with session() as conn:
        ids = matching_ids(conn, replace(_filter_from(request), user_id=user_id))
        count, batch = bulk_pin(conn, user_id, ids)
        total = pinned_count(conn, user_id)
    return JSONResponse({"pinned": count, "batch": batch, "pinned_total": total})


@app.post("/api/series/bulk-undo")
async def bulk_undo(request: Request) -> JSONResponse:
    user_id = int(request.state.user["id"])
    with session() as conn:
        batch = latest_bulk_batch(conn, user_id)
        undone = undo_bulk_pin(conn, user_id, batch) if batch else 0
        total = pinned_count(conn, user_id)
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


def _calendar_context(user_id: int, month: str | None) -> dict[str, Any]:
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
        rows = pinned_episodes(
            conn,
            user_id,
            window_start.isoformat(),
            window_end.isoformat(),
            include_unmonitored=get_settings().show_unmonitored,
            include_specials=get_settings().show_specials,
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


@app.get("/api/calendar/live")
async def calendar_live(request: Request) -> JSONResponse:
    """Current state of everything on the user's calendar window.

    Small enough to poll: the page holds the layout and only swaps the parts
    that can change while you are looking at it.
    """
    user_id = int(request.state.user["id"])
    now, tz = _now_local()
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


@app.get("/api/calendar")
async def calendar_json(
    request: Request, start: str | None = None, end: str | None = None
) -> JSONResponse:
    """Pinned episodes in a window, with derived state. SPEC §12."""
    now, tz = _now_local()
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


@app.post("/api/episodes/{episode_id}/why")
async def episode_diagnosis(episode_id: int) -> JSONResponse:
    """Ask Sonarr why this hasn't turned up."""
    return JSONResponse(await why_missing(episode_id))


@app.post("/api/episodes/{episode_id}/search")
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


@app.get("/ready")
async def ready(request: Request, fits: str = ""):
    """What has landed and is waiting, rather than what is coming.

    The calendar is built around anticipation. This is the other half: for
    anyone who watches after a download rather than on transmission, "what
    can I put on now" is the question they actually have.
    """
    user_id = int(request.state.user["id"])
    now, tz = _now_local()
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


@app.get("/gaps")
async def gaps_page(request: Request):
    """Holes in the shows you follow."""
    user_id = int(request.state.user["id"])
    now, tz = _now_local()
    with session() as conn:
        grouped = gaps(conn, user_id)
        shortfall = plex_shortfall(conn, user_id)
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
        },
    )


@app.get("/retire")
async def retire_page(request: Request, done: str = ""):
    """Pins that can never produce another episode.

    §10 makes this argument for dormant shows; it applies at least as
    strongly to ones that genuinely ended. A pin that cannot produce another
    episode is not a subscription, it is a souvenir.
    """
    user_id = int(request.state.user["id"])
    with session() as conn:
        candidates = finished_pins(conn, user_id)
    return templates.TemplateResponse(
        request, "retire.html", {"candidates": candidates, "flash": done}
    )


@app.post("/api/series/retire")
async def retire_pins(request: Request) -> JSONResponse:
    """Unpin everything the retire page offered, re-running the query rather
    than trusting a list of ids from the browser."""
    user_id = int(request.state.user["id"])
    with session() as conn:
        ids = [int(r["id"]) for r in finished_pins(conn, user_id)]
        removed, batch = retire(conn, user_id, ids)
        total = pinned_count(conn, user_id)
    return JSONResponse({"retired": removed, "batch": batch, "pinned_total": total})


@app.get("/discover")
async def discover(request: Request):
    """Unpinned series with something actually coming.

    A 2000-series library is mostly things you have forgotten about. Every
    pin so far has required you to remember a show exists; this is the view
    that does the remembering.
    """
    user_id = int(request.state.user["id"])
    now, tz = _now_local()
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


@app.post("/api/series/{series_id}/episodes")
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


@app.get("/series/{series_id}")
async def series_detail(request: Request, series_id: int):
    now, tz = _now_local()
    with session() as conn:
        row = get_series(conn, series_id, int(request.state.user["id"]))
        if row is None:
            raise HTTPException(status_code=404, detail="no such series")
        seasons = episodes_by_season(conn, series_id)
        progress = season_progress(conn, series_id)
        genres = genres_for(conn, series_id)

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
            "open_season": latest_season(seasons),
            "progress": progress,
        },
    )
