"""Two-way sync between pins and each user's Plex Watchlist.

Same shape as the Sonarr tag sync, and the same hard part: "pinned here but
not listed there" means either *add it to the watchlist* or *unpin it here*,
and guessing wrong silently undoes what someone just did. The last observed
state decides which side moved.

Where it differs is what can be matched at all. A watchlist entry is a Plex
Discover object, so it can only become a pin if that show is already in your
library — otherwise there is nothing to pin, no episodes and no Sonarr entry.
Those are reported rather than turned into hollow pins.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients import watchlist
from app.clients.http import UpstreamError
from app.config import get_settings
from app.db import session, utcnow
from app.jobs import tracked
from app.repo import set_pinned

log = logging.getLogger(__name__)


def _state(conn: Any, user_id: int) -> dict[int, tuple[int, int]]:
    return {
        int(r["series_id"]): (int(r["pinned"]), int(r["listed"]))
        for r in conn.execute(
            "SELECT series_id, pinned, listed FROM watchlist_sync_state WHERE user_id = ?",
            (user_id,),
        )
    }


def _remember(conn: Any, user_id: int, series_id: int, pinned: bool, listed: bool) -> None:
    conn.execute(
        "INSERT INTO watchlist_sync_state (user_id, series_id, pinned, listed, synced_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, series_id) DO UPDATE SET "
        "pinned = excluded.pinned, listed = excluded.listed, synced_at = excluded.synced_at",
        (user_id, series_id, int(pinned), int(listed), utcnow()),
    )


async def _sync_user(user: Any) -> str:
    token = user["plex_token"]
    if not token:
        return f"{user['username']}: no Plex token"

    items = await watchlist.fetch(token)
    listed_by_guid = {item.guid: item for item in items}

    with session() as conn:
        by_guid = {
            str(r["plex_guid"]): int(r["id"])
            for r in conn.execute(
                "SELECT id, plex_guid FROM series WHERE plex_guid IS NOT NULL"
            )
        }
        keys = {
            int(r["id"]): watchlist.rating_key_from_guid(r["plex_guid"])
            for r in conn.execute(
                "SELECT id, plex_guid FROM series WHERE plex_guid IS NOT NULL"
            )
        }
        pinned_ids = {
            int(r["series_id"])
            for r in conn.execute(
                "SELECT series_id FROM pins WHERE user_id = ?", (user["id"],)
            )
        }
        previous = _state(conn, int(user["id"]))

    listed_ids = {by_guid[g] for g in listed_by_guid if g in by_guid}
    unmatched = len(listed_by_guid) - len(listed_ids)

    to_list: list[int] = []
    to_unlist: list[int] = []
    pin_here: list[int] = []
    unpin_here: list[int] = []

    for series_id in pinned_ids | listed_ids | set(previous):
        pinned_now = series_id in pinned_ids
        listed_now = series_id in listed_ids
        was_pinned, was_listed = previous.get(series_id, (0, 0))

        if pinned_now == listed_now:
            continue

        pin_changed = pinned_now != bool(was_pinned)
        list_changed = listed_now != bool(was_listed)

        if pin_changed or not list_changed:
            # A pin with no plex:// identity cannot be watchlisted — nothing
            # in Discover corresponds to it.
            if pinned_now and not keys.get(series_id):
                continue
            (to_list if pinned_now else to_unlist).append(series_id)
        else:
            (pin_here if listed_now else unpin_here).append(series_id)

    for series_id in to_list:
        with_key = keys.get(series_id)
        if with_key:
            await watchlist.add(token, with_key)
    for series_id in to_unlist:
        with_key = keys.get(series_id)
        if with_key:
            await watchlist.remove(token, with_key)

    with session() as conn:
        for series_id in pin_here:
            set_pinned(conn, int(user["id"]), series_id, True)
        for series_id in unpin_here:
            set_pinned(conn, int(user["id"]), series_id, False)

        final = (pinned_ids | set(pin_here) | set(to_list)) - set(unpin_here)
        for series_id in pinned_ids | listed_ids | set(previous):
            here = series_id in final
            _remember(conn, int(user["id"]), series_id, here, here)

    changes = len(to_list) + len(to_unlist) + len(pin_here) + len(unpin_here)
    note = f"{user['username']}: "
    note += "in step" if not changes else (
        f"+{len(to_list)}/-{len(to_unlist)} watchlist, "
        f"+{len(pin_here)}/-{len(unpin_here)} pins"
    )
    if unmatched:
        note += f" ({unmatched} watchlisted show(s) not in your library)"
    return note


@tracked("plex_watchlist")
async def sync_watchlist() -> str:
    if not get_settings().plex_watchlist_sync:
        return "skipped: watchlist sync is off"

    with session() as conn:
        users = list(
            conn.execute(
                "SELECT id, username, plex_token FROM users "
                "WHERE plex_token IS NOT NULL AND plex_token != '' ORDER BY id"
            )
        )

    if not users:
        return "skipped: nobody has a Plex token"

    notes = []
    for user in users:
        try:
            notes.append(await _sync_user(user))
        except UpstreamError as exc:
            log.warning("watchlist sync failed for %s: %s", user["username"], exc)
            notes.append(f"{user['username']}: {exc}")
        except Exception as exc:  # noqa: BLE001 — undocumented API, one user's
            log.exception("watchlist sync blew up for %s", user["username"])
            notes.append(f"{user['username']}: {type(exc).__name__}")

    return "; ".join(notes)
