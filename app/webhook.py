"""Sonarr webhook receiver.

The payload shape is not documented in the Servarr wiki (SPEC §17, open
question 1), so this parser is written to be wrong safely: anything it does
not recognise is recorded and acknowledged rather than raised. A webhook that
500s gets disabled by Sonarr after enough failures, which would take the
feature down permanently to report a problem with one delivery.

Every delivery is stored, so the real shape can be read off a live Test
button instead of guessed at.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app.db import session, utcnow
from app.jobs.notifications import notify_arrival

log = logging.getLogger(__name__)

#: Deliveries worth keeping. Enough to debug a misfire, not enough to grow
#: without bound on a busy library.
KEEP_DELIVERIES = 50

#: Sonarr fires On Import and On Upgrade as the same event type, separated by
#: isUpgrade. Both mean "the file is now there", which is what we notify on.
ARRIVAL_EVENTS = frozenset({"download", "episodefileimport", "import"})


@dataclass
class Delivery:
    event_type: str
    tvdb_id: int | None = None
    sonarr_series_id: int | None = None
    title: str | None = None
    is_upgrade: bool = False
    episodes: list[tuple[int, int]] = field(default_factory=list)


def parse(payload: Any) -> Delivery:
    """Pull what we need out of a payload, tolerating anything else.

    Sonarr has changed key casing between versions, so lookups are forgiving
    rather than exact.
    """
    if not isinstance(payload, dict):
        return Delivery(event_type="unknown")

    series = payload.get("series") or {}
    if not isinstance(series, dict):
        series = {}

    episodes: list[tuple[int, int]] = []
    for item in payload.get("episodes") or []:
        if not isinstance(item, dict):
            continue
        season = item.get("seasonNumber")
        number = item.get("episodeNumber")
        if season is None or number is None:
            continue
        try:
            episodes.append((int(season), int(number)))
        except (TypeError, ValueError):
            continue

    def as_int(value: Any) -> int | None:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return Delivery(
        event_type=str(payload.get("eventType") or "unknown").strip().lower(),
        tvdb_id=as_int(series.get("tvdbId")),
        sonarr_series_id=as_int(series.get("id")),
        title=series.get("title"),
        is_upgrade=bool(payload.get("isUpgrade")),
        episodes=episodes,
    )


def resolve_series(conn: sqlite3.Connection, delivery: Delivery) -> int | None:
    """TVDB id first, per SPEC §6. Sonarr's own id is the fallback."""
    if delivery.tvdb_id:
        row = conn.execute(
            "SELECT id FROM series WHERE tvdb_id = ?", (delivery.tvdb_id,)
        ).fetchone()
        if row:
            return int(row["id"])
    if delivery.sonarr_series_id:
        row = conn.execute(
            "SELECT id FROM series WHERE sonarr_id = ?", (delivery.sonarr_series_id,)
        ).fetchone()
        if row:
            return int(row["id"])
    return None


def mark_arrived(conn: sqlite3.Connection, series_id: int, season: int, episode: int) -> bool:
    """Record that Sonarr now holds the file.

    arrived_at is stamped once and never moved, so an upgrade from 720p to
    1080p does not make an old episode look newly arrived.
    """
    cur = conn.execute(
        "UPDATE episodes SET has_file = 1, arrived_at = COALESCE(arrived_at, ?), "
        "updated_at = ? WHERE series_id = ? AND season = ? AND episode = ?",
        (utcnow(), utcnow(), series_id, season, episode),
    )
    return cur.rowcount > 0


def record(event_type: str, handled: bool, detail: str, body: str) -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO webhook_log (received_at, event_type, handled, detail, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (utcnow(), event_type, int(handled), detail[:500], body[:8000]),
        )
        conn.execute(
            "DELETE FROM webhook_log WHERE id NOT IN "
            "(SELECT id FROM webhook_log ORDER BY id DESC LIMIT ?)",
            (KEEP_DELIVERIES,),
        )


def recent(limit: int = 20) -> list[sqlite3.Row]:
    with session() as conn:
        return list(
            conn.execute(
                "SELECT * FROM webhook_log ORDER BY id DESC LIMIT ?", (limit,)
            )
        )


async def handle(payload: Any, raw: str) -> str:
    """Process one delivery and return a one-line description of what happened.

    Never raises. The description is stored and shown in the panel, so an
    unrecognised payload leaves a trail rather than a silence.
    """
    delivery = parse(payload)

    if delivery.event_type == "test":
        record("test", True, "Test delivery received — the webhook is wired up.", raw)
        return "test acknowledged"

    if delivery.event_type not in ARRIVAL_EVENTS:
        detail = f"ignored: nothing to do for {delivery.event_type!r}"
        record(delivery.event_type, False, detail, raw)
        return detail

    with session() as conn:
        series_id = resolve_series(conn, delivery)

    if series_id is None:
        detail = (
            f"no local series for {delivery.title or 'unknown'} "
            f"(tvdb {delivery.tvdb_id}) — run the Plex and Sonarr syncs"
        )
        record(delivery.event_type, False, detail, raw)
        log.warning("webhook: %s", detail)
        return detail

    if not delivery.episodes:
        detail = "arrival with no episodes in the payload"
        record(delivery.event_type, False, detail, raw)
        return detail

    pushed = 0
    with session() as conn:
        for season, number in delivery.episodes:
            mark_arrived(conn, series_id, season, number)

    for season, number in delivery.episodes:
        pushed += await notify_arrival(series_id, season, number)

    what = "upgrade" if delivery.is_upgrade else "import"
    detail = (
        f"{delivery.title or series_id}: {len(delivery.episodes)} episode(s) marked "
        f"present on {what}, {pushed} notification(s) sent"
    )
    record(delivery.event_type, True, detail, raw)
    return detail


def payload_from(raw: bytes) -> Any:
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return None
