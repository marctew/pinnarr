"""Two-way sync between pins and Sonarr tags.

Each account gets a tag — `pinnarr-marc` — carrying exactly that person's
pins. A single shared tag could not express whose pin it was, and pins are
per user.

The hard part is direction. "Pinned here but not tagged there" is ambiguous:
it means either *pin it in Sonarr* or *unpin it here*, and guessing wrong
silently undoes whatever someone just did. So the last observed state of each
pair is recorded, and the side that changed since then is the side that wins.
When both changed, Pinnarr wins — pinning is a deliberate act here and a side
effect there.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.clients.http import UpstreamError
from app.clients.sonarr import SonarrClient
from app.config import get_settings
from app.db import session, utcnow
from app.jobs import tracked
from app.repo import set_pinned

log = logging.getLogger(__name__)

TAG_PREFIX = "pinnarr-"


def tag_label(username: str) -> str:
    """Sonarr lowercases tags and dislikes spaces, so normalise to match."""
    slug = re.sub(r"[^a-z0-9]+", "-", username.strip().lower()).strip("-")
    return f"{TAG_PREFIX}{slug or 'user'}"


def _state(conn: Any, user_id: int) -> dict[int, tuple[int, int]]:
    return {
        int(r["series_id"]): (int(r["pinned"]), int(r["tagged"]))
        for r in conn.execute(
            "SELECT series_id, pinned, tagged FROM tag_sync_state WHERE user_id = ?",
            (user_id,),
        )
    }


def _remember(conn: Any, user_id: int, series_id: int, pinned: bool, tagged: bool) -> None:
    conn.execute(
        "INSERT INTO tag_sync_state (user_id, series_id, pinned, tagged, synced_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, series_id) DO UPDATE SET "
        "pinned = excluded.pinned, tagged = excluded.tagged, synced_at = excluded.synced_at",
        (user_id, series_id, int(pinned), int(tagged), utcnow()),
    )


async def _sync_user(
    client: SonarrClient,
    tags: dict[str, int],
    series_tags: dict[int, set[int]],
    user: Any,
) -> str:
    label = tag_label(user["username"])
    tag_id = tags.get(label)

    with session() as conn:
        pinned_ids = {
            int(r["series_id"])
            for r in conn.execute(
                "SELECT p.series_id FROM pins p JOIN series s ON s.id = p.series_id "
                "WHERE p.user_id = ? AND s.sonarr_id IS NOT NULL",
                (user["id"],),
            )
        }

    if tag_id is None:
        if not pinned_ids:
            return f"{user['username']}: nothing to mirror"
        tag_id = await client.create_tag(label)
        tags[label] = tag_id

    tagged_sonarr_ids = {
        sonarr_id for sonarr_id, on_it in series_tags.items() if tag_id in on_it
    }

    with session() as conn:
        # Sonarr ids are what the tag speaks; local ids are what pins do.
        by_sonarr = {
            int(r["sonarr_id"]): int(r["id"])
            for r in conn.execute(
                "SELECT id, sonarr_id FROM series WHERE sonarr_id IS NOT NULL"
            )
        }
        to_sonarr = {v: k for k, v in by_sonarr.items()}
        previous = _state(conn, int(user["id"]))

    tagged_ids = {by_sonarr[s] for s in tagged_sonarr_ids if s in by_sonarr}

    add_tag: list[int] = []
    drop_tag: list[int] = []
    pin_here: list[int] = []
    unpin_here: list[int] = []

    for series_id in pinned_ids | tagged_ids | set(previous):
        pinned_now = series_id in pinned_ids
        tagged_now = series_id in tagged_ids
        was_pinned, was_tagged = previous.get(series_id, (0, 0))

        if pinned_now == tagged_now:
            continue

        pin_changed = pinned_now != bool(was_pinned)
        tag_changed = tagged_now != bool(was_tagged)

        # Both sides moved: Pinnarr wins. Pinning is deliberate here and a
        # side effect of housekeeping there.
        if pin_changed or not tag_changed:
            (add_tag if pinned_now else drop_tag).append(series_id)
        else:
            (pin_here if tagged_now else unpin_here).append(series_id)

    if add_tag:
        await client.apply_tag([to_sonarr[i] for i in add_tag], tag_id, add=True)
    if drop_tag:
        await client.apply_tag([to_sonarr[i] for i in drop_tag], tag_id, add=False)

    with session() as conn:
        for series_id in pin_here:
            set_pinned(conn, int(user["id"]), series_id, True)
        for series_id in unpin_here:
            set_pinned(conn, int(user["id"]), series_id, False)

        final_pinned = (pinned_ids | set(pin_here) | set(add_tag)) - set(unpin_here)
        for series_id in pinned_ids | tagged_ids | set(previous):
            here = series_id in final_pinned
            _remember(conn, int(user["id"]), series_id, here, here)

    changes = len(add_tag) + len(drop_tag) + len(pin_here) + len(unpin_here)
    if not changes:
        return f"{user['username']}: in step"
    return (
        f"{user['username']}: +{len(add_tag)}/-{len(drop_tag)} tags, "
        f"+{len(pin_here)}/-{len(unpin_here)} pins"
    )


@tracked("sonarr_tags")
async def sync_tags() -> str:
    settings = get_settings()
    if not settings.sonarr_configured:
        return "skipped: Sonarr not configured"
    if not settings.sonarr_tag_sync:
        return "skipped: tag sync is off"

    client = SonarrClient()
    try:
        tags = await client.tags()
        series_tags = await client.series_tag_map()
    except UpstreamError as exc:
        return f"error: {exc}"

    with session() as conn:
        users = list(conn.execute("SELECT id, username FROM users ORDER BY id"))

    notes = []
    for user in users:
        try:
            notes.append(await _sync_user(client, tags, series_tags, user))
        except UpstreamError as exc:
            log.warning("tag sync failed for %s: %s", user["username"], exc)
            notes.append(f"{user['username']}: {exc}")

    return "; ".join(notes) or "no users"
