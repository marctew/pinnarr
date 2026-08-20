"""Accounts, passwords and sessions.

No new dependencies: scrypt ships with hashlib and is a sound choice for
password storage. Parameters are recorded in the stored string so they can be
raised later without invalidating existing hashes.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from app.db import session, utcnow

log = logging.getLogger(__name__)

COOKIE: Final = "pinnarr_session"
SESSION_DAYS: Final = 30

#: scrypt cost. n=2**15 keeps a login around a tenth of a second on the sort
#: of hardware this runs on, which is the right trade for a LAN app.
_N: Final = 2**15
_R: Final = 8
_P: Final = 1


def _maxmem(n: int, r: int) -> int:
    """scrypt needs 128*n*r bytes, and OpenSSL refuses above 32MB unless told
    otherwise. At n=2**15, r=8 that is ~33MB — just over the default, so the
    limit has to be raised explicitly or every hash raises ValueError."""
    return 128 * n * r * 2

ADMIN: Final = "admin"
USER: Final = "user"
ROLES: Final = (ADMIN, USER)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32, maxmem=_maxmem(_N, _R)
    )
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, want = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(want) // 2,
            maxmem=_maxmem(int(n), int(r)),
        )
    except (ValueError, TypeError):
        return False
    # Constant time: a timing oracle on password comparison is cheap to avoid.
    return hmac.compare_digest(dk.hex(), want)


# ── Users ────────────────────────────────────────


def user_count() -> int:
    with session() as conn:
        return int(conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"])


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE username = ?", (username.strip(),)
    ).fetchone()


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM users ORDER BY role, username"))


def create_user(
    conn: sqlite3.Connection, username: str, password: str, role: str = USER
) -> int:
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, role, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (username.strip(), hash_password(password), role if role in ROLES else USER, now, now),
    )
    return int(cur.lastrowid or 0)


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    conn.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (hash_password(password), utcnow(), user_id),
    )
    # Every other session for this user dies with the old password.
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def set_role(conn: sqlite3.Connection, user_id: int, role: str) -> None:
    conn.execute(
        "UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
        (role if role in ROLES else USER, utcnow(), user_id),
    )


def set_topic(conn: sqlite3.Connection, user_id: int, topic: str) -> None:
    conn.execute(
        "UPDATE users SET ntfy_topic = ?, updated_at = ? WHERE id = ?",
        (topic.strip() or None, utcnow(), user_id),
    )


def set_plex_token(conn: sqlite3.Connection, user_id: int, token: str) -> None:
    """The account's own Plex token, for its own watchlist."""
    conn.execute(
        "UPDATE users SET plex_token = ?, updated_at = ? WHERE id = ?",
        (token.strip() or None, utcnow(), user_id),
    )


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def admin_count(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT count(*) AS n FROM users WHERE role = ?", (ADMIN,)
        ).fetchone()["n"]
    )


# ── Sessions ─────────────────────────────────────


def start_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(days=SESSION_DAYS)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (token, user_id, utcnow(), expires.replace(microsecond=0).isoformat()),
    )
    return token


def end_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def user_for_token(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM sessions sess JOIN users u ON u.id = sess.user_id "
        "WHERE sess.token = ? AND sess.expires_at > ?",
        (token, utcnow()),
    ).fetchone()
    return row


def purge_expired(conn: sqlite3.Connection) -> int:
    return conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (utcnow(),)).rowcount


def authenticate(username: str, password: str) -> Any | None:
    with session() as conn:
        user = by_username(conn, username)
    if user is None:
        # Hash anyway, so a missing username and a wrong password take the
        # same time and the login form can't be used to enumerate accounts.
        hash_password(password)
        return None
    return user if verify_password(password, user["password_hash"]) else None
