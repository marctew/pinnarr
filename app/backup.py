"""Backup and restore.

Almost everything in the database is derived: series, episodes, genres and
outlooks all rebuild from Plex, Sonarr and TMDB in a few minutes. Three
things do not exist anywhere else, and losing them means redoing work by
hand — accounts, settings, and pins.

Pins are exported by TVDB id rather than by row id, because row ids are local
to one database and a restore into a fresh install would silently attach every
pin to the wrong show.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings, get_settings, save_settings
from app.db import all_settings, session, utcnow
from app.repo import refresh_pinned_flag

log = logging.getLogger(__name__)

FORMAT_VERSION = 1


def export() -> dict[str, Any]:
    """Everything that cannot be rebuilt from the upstreams.

    Includes secrets and password hashes, because a backup that omits them
    is not a backup — it is a partial one that fails at the worst moment.
    Callers must treat the result as sensitive.
    """
    with session() as conn:
        users = [
            {
                "username": r["username"],
                "password_hash": r["password_hash"],
                "role": r["role"],
                "ntfy_topic": r["ntfy_topic"],
            }
            for r in conn.execute("SELECT * FROM users ORDER BY id")
        ]
        pins = [
            {
                "username": r["username"],
                "tvdb_id": r["tvdb_id"],
                "title": r["title"],
                "year": r["year"],
                "notify": int(r["notify"]),
                "pinned_at": r["pinned_at"],
            }
            for r in conn.execute(
                """
                SELECT u.username, s.tvdb_id, s.title, s.year, p.notify, p.pinned_at
                FROM pins p
                JOIN users u ON u.id = p.user_id
                JOIN series s ON s.id = p.series_id
                ORDER BY u.username, s.sort_title
                """
            )
        ]

    return {
        "format": FORMAT_VERSION,
        "exported_at": utcnow(),
        "settings": all_settings(),
        "users": users,
        "pins": pins,
    }


def _resolve_series(conn: Any, pin: dict[str, Any]) -> int | None:
    """Find the local row for an exported pin. TVDB id first, per SPEC §6."""
    if pin.get("tvdb_id"):
        row = conn.execute(
            "SELECT id FROM series WHERE tvdb_id = ?", (pin["tvdb_id"],)
        ).fetchone()
        if row:
            return int(row["id"])
    if pin.get("title"):
        row = conn.execute(
            "SELECT id FROM series WHERE title = ? AND (year IS ? OR year = ?)",
            (pin["title"], pin.get("year"), pin.get("year")),
        ).fetchone()
        if row:
            return int(row["id"])
    return None


def restore(payload: Any) -> dict[str, Any]:
    """Merge a backup into the current database.

    Deliberately additive: existing accounts and pins are left alone and
    missing ones are added. A restore that wiped what was already there would
    be a far worse mistake to make by accident than an incomplete merge.

    Settings are the exception — they are overwritten, since a half-merged
    configuration is not a configuration.
    """
    if not isinstance(payload, dict) or payload.get("format") != FORMAT_VERSION:
        raise ValueError(f"not a Pinnarr backup of format {FORMAT_VERSION}")

    report = {"users": 0, "pins": 0, "unmatched": [], "settings": 0}

    stored = payload.get("settings") or {}
    known = {k: v for k, v in stored.items() if k in Settings.model_fields}
    if known:
        save_settings(known)
        report["settings"] = len(known)

    with session() as conn:
        for user in payload.get("users") or []:
            username = (user.get("username") or "").strip()
            if not username or not user.get("password_hash"):
                continue
            if conn.execute(
                "SELECT 1 FROM users WHERE username = ?", (username,)
            ).fetchone():
                continue
            now = utcnow()
            conn.execute(
                "INSERT INTO users (username, password_hash, role, ntfy_topic, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (username, user["password_hash"], user.get("role") or "user",
                 user.get("ntfy_topic"), now, now),
            )
            report["users"] += 1

        ids = {r["username"]: int(r["id"]) for r in conn.execute("SELECT id, username FROM users")}
        touched: set[int] = set()

        for pin in payload.get("pins") or []:
            user_id = ids.get(pin.get("username"))
            if user_id is None:
                continue
            series_id = _resolve_series(conn, pin)
            if series_id is None:
                # The show is not in this library yet. Report it rather than
                # dropping it silently — usually it means a sync is pending.
                report["unmatched"].append(pin.get("title") or str(pin.get("tvdb_id")))
                continue
            cur = conn.execute(
                "INSERT INTO pins (user_id, series_id, pinned_at, notify) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(user_id, series_id) DO NOTHING",
                (user_id, series_id, pin.get("pinned_at") or utcnow(), int(pin.get("notify", 1))),
            )
            if cur.rowcount:
                report["pins"] += 1
                touched.add(series_id)

        for series_id in touched:
            refresh_pinned_flag(conn, series_id)

    get_settings.cache_clear()
    return report
