"""Settings, backup, job control and health. Admin-only, bar /healthz."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import ValidationError

from app import __version__, backup
from app.config import (
    SCHEDULING_FIELDS,
    SECRET_FIELDS,
    Settings,
    get_settings,
    save_settings,
)
from app.db import last_runs, utcnow
from app.health import test_service
from app.jobs import REGISTRY
from app.scheduling import restart_scheduler
from app.web import templates

log = logging.getLogger(__name__)

router = APIRouter()

#: One lock per job name, so a manual trigger can't race the scheduler or a
#: second impatient click. The cron side gets this from max_instances=1.
_job_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@router.get("/settings/backup")
async def backup_page(request: Request, restored: str = "", error: str = ""):
    return templates.TemplateResponse(
        request, "backup.html",
        {"flash": error or restored, "flash_kind": "bad" if error else "ok"},
    )


@router.get("/api/backup")
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


@router.post("/settings/backup")
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


@router.get("/settings/jobs")
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


@router.get("/healthz")
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


@router.post("/api/sync/{job}")
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


@router.get("/settings")
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


@router.post("/settings")
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


@router.post("/api/settings/test/{service}")
async def test_connection(service: str) -> JSONResponse:
    """Try one integration against the saved settings and report back."""
    return JSONResponse(await test_service(service))
