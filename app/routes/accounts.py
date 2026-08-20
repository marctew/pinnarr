"""Signing in, your own account, and the admin account list."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app import auth, notify
from app.db import session
from app.repo import (
    adopt_orphaned_pins,
)
from app.web import templates

log = logging.getLogger(__name__)

router = APIRouter()


# ── Sign in ──────────────────────────────────────


@router.get("/setup")
async def setup_form(request: Request, error: str = ""):
    if auth.user_count():
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"error": error})


@router.post("/setup")
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


@router.get("/login")
async def login_form(request: Request, error: str = "", next: str = "/"):
    if not auth.user_count():
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": error, "next": next})


@router.post("/login")
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


@router.post("/logout")
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


@router.get("/profile")
async def profile(request: Request, saved: str = "", error: str = "", new_key: str = ""):
    with session() as conn:
        keys = auth.api_keys(conn, int(request.state.user["id"]))
    return templates.TemplateResponse(
        request, "profile.html",
        {
            "flash": error or ("Saved." if saved else ""),
            "flash_kind": "bad" if error else "ok",
            "keys": keys,
            # Shown once, on the redirect that created it, and never again.
            "new_key": new_key,
        },
    )


@router.post("/profile/keys")
async def create_key(request: Request) -> RedirectResponse:
    """Mint an API key for something that is not a browser.

    Redirect rather than JSON so the key survives exactly one page render
    and then cannot be got at again — including by whoever walks up to the
    machine next.
    """
    form = await request.form()
    name = str(form.get("name", "")).strip()[:60]
    with session() as conn:
        key = auth.create_api_key(conn, int(request.state.user["id"]), name)
    return RedirectResponse(f"/profile?new_key={quote(key)}", status_code=303)


@router.post("/profile/keys/{key_id}/revoke")
async def revoke_key(request: Request, key_id: int) -> RedirectResponse:
    with session() as conn:
        auth.revoke_api_key(conn, int(request.state.user["id"]), key_id)
    return RedirectResponse("/profile?saved=1", status_code=303)


@router.get("/notifications")
async def notification_history(request: Request, scope: str = "mine"):
    """What Pinnarr actually pushed, and whether ntfy took it.

    Without this, a notification that does not arrive has three
    indistinguishable explanations: the job never fired, ntfy refused it, or
    the phone ate it. The first two are knowable.
    """
    user = request.state.user
    is_admin = user["role"] == auth.ADMIN
    everyone = is_admin and scope == "all"
    with session() as conn:
        rows = notify.history(conn, None if everyone else int(user["id"]))

    return templates.TemplateResponse(
        request,
        "notifications.html",
        {
            "rows": rows,
            "kinds": notify.KINDS,
            "is_admin": is_admin,
            "scope": "all" if everyone else "mine",
        },
    )


@router.post("/profile")
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


@router.get("/settings/users")
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


@router.post("/settings/users")
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
