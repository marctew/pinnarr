"""The Sonarr webhook, and the panel that explains how to point it here."""

from __future__ import annotations

import logging
from hmac import compare_digest

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app import auth
from app import webhook as hooks
from app.clients import watchlist as watchlist_client
from app.config import (
    get_settings,
)
from app.db import session, utcnow
from app.repo import (
    is_pinned_by,
    set_notify,
)
from app.web import templates

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/hooks/sonarr")
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


@router.get("/settings/webhook")
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


@router.post("/api/series/{series_id}/notify")
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


@router.post("/api/profile/watchlist-test")
async def watchlist_test(request: Request) -> JSONResponse:
    """Check the signed-in user's own Plex token against their watchlist."""
    with session() as conn:
        row = auth.get_user(conn, int(request.state.user["id"]))

    result = await watchlist_client.check(row["plex_token"] or "")
    # Learn who the token belongs to while we have it: Tautulli reports
    # history by Plex username, and without it nothing can be attributed.
    if result.get("username"):
        with session() as conn:
            conn.execute(
                "UPDATE users SET plex_username = ?, updated_at = ? WHERE id = ?",
                (result["username"], utcnow(), int(request.state.user["id"])),
            )
    return JSONResponse(result)
