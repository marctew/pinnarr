"""Sending a notification, and writing down that it was sent.

One seam in front of the ntfy client. Every push goes through here, so the
log is complete by construction rather than by remembering to add a line at
each call site — which is the failure mode that makes a log worth less than
no log at all.

The client stays a plain publisher with no knowledge of the database, so
swapping ntfy for Pushover or Gotify is still a new module and a config line.
"""

from __future__ import annotations

import logging

from app.clients import ntfy
from app.db import session, utcnow

log = logging.getLogger(__name__)

#: What a push was for. Shown as a filter on the history page.
KINDS = {
    "arrival": "Episode arrived",
    "digest": "Weekly digest",
    "schedule": "Air date moved",
    "season": "New season",
    "test": "Test",
}


async def send(
    title: str,
    message: str,
    *,
    kind: str,
    user_id: int | None = None,
    tags: str = "tv",
    priority: str = "default",
    click: str | None = None,
    topic: str | None = None,
) -> bool:
    """Publish one notification and record the attempt. Never raises."""
    ok = await ntfy.send(
        title, message, tags=tags, priority=priority, click=click, topic=topic
    )
    try:
        with session() as conn:
            conn.execute(
                "INSERT INTO notification_log (user_id, topic, kind, title, body, "
                "ok, detail, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, topic, kind, title, message, int(ok),
                 None if ok else "ntfy did not accept it — see the log for why",
                 utcnow()),
            )
    except Exception:  # noqa: BLE001 — a bookkeeping failure must not eat a push
        log.exception("could not record notification %r", title)
    return ok


def history(conn, user_id: int | None = None, *, limit: int = 200) -> list:
    """Most recent first. `user_id` None means everyone, for an admin."""
    if user_id is None:
        return list(
            conn.execute(
                "SELECT n.*, u.username FROM notification_log n "
                "LEFT JOIN users u ON u.id = n.user_id "
                "ORDER BY n.id DESC LIMIT ?", (limit,)
            )
        )
    return list(
        conn.execute(
            "SELECT n.*, u.username FROM notification_log n "
            "LEFT JOIN users u ON u.id = n.user_id "
            "WHERE n.user_id = ? ORDER BY n.id DESC LIMIT ?", (user_id, limit)
        )
    )
