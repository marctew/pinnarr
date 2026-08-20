"""Pinning here must never be undone by a sync that had nothing to say.

Both two-way syncs decide direction from the last observed state: whichever
side changed since then is the side that moved. That only works if the
remembered state is what was actually *observed*. Recording what we intended
instead makes the next run read our own failed write as somebody else's
deliberate removal — and delete the pin.

This is the failure it produced in practice: pin something, wait ten minutes,
find it unpinned.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.clients import watchlist as watchlist_client
from app.config import save_settings
from app.db import session, utcnow
from app.jobs.watchlist_sync import sync_watchlist
from app.main import app
from tests.factories import make_series

GUID = "plex://show/5d9c0874ffd9ef001e99607a"


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        save_settings({"plex_watchlist_sync": "true"})
        yield c


@pytest.fixture
def plex(monkeypatch):
    """A fake Plex Watchlist that can be told to ignore writes.

    Which it does in real life: the endpoints are undocumented, they accept
    a PUT and return success, and the item does not always appear.
    """
    state: dict[str, object] = {"listed": set(), "accept_writes": True, "adds": []}

    async def fake_fetch(_token):
        return [
            watchlist_client.WatchlistItem(
                rating_key=g.rsplit("/", 1)[-1], guid=g, title="Silo", kind="show"
            )
            for g in state["listed"]
        ]

    async def fake_add(_token, rating_key):
        state["adds"].append(rating_key)
        if state["accept_writes"]:
            state["listed"].add(f"plex://show/{rating_key}")

    async def fake_remove(_token, rating_key):
        state["listed"].discard(f"plex://show/{rating_key}")

    for name, fn in (("fetch", fake_fetch), ("add", fake_add), ("remove", fake_remove)):
        monkeypatch.setattr(watchlist_client, name, fn)
    return state


def pin_one(user_id, *, guid=GUID):
    with session() as conn:
        conn.execute("UPDATE users SET plex_token = 'tok' WHERE id = ?", (user_id,))
        sid = make_series(conn, "Silo", plex_guid=guid, sonarr_id=7)
        conn.execute(
            "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
            (user_id, sid, utcnow()),
        )
        conn.execute("UPDATE series SET pinned = 1 WHERE id = ?", (sid,))
    return sid


def pinned():
    with session() as conn:
        return {int(r["series_id"]) for r in conn.execute("SELECT series_id FROM pins")}


def remembered(series_id):
    with session() as conn:
        row = conn.execute(
            "SELECT pinned, listed FROM watchlist_sync_state WHERE series_id = ?",
            (series_id,),
        ).fetchone()
    return None if row is None else (row["pinned"], row["listed"])


# ── The bug that was ──


async def test_a_pin_with_no_plex_identity_survives_repeated_syncs(
    client, admin_token, plex
):
    """The one that bit. A series Plex matched with a legacy agent has no
    plex:// guid, so it cannot be watchlisted at all — the sync says so and
    skips it. It used to then record the skip as a successful listing, and
    the next run read listed 1 → 0 as a removal in Plex."""
    _, user_id = admin_token
    sid = pin_one(user_id, guid=None)

    for _ in range(3):
        await sync_watchlist()
        assert pinned() == {sid}
    assert remembered(sid) == (1, 0)


async def test_the_skip_is_reported_every_run_not_just_the_first(
    client, admin_token, plex
):
    """Silence would read as "in step", which is exactly the lie that made
    the unpin invisible."""
    _, user_id = admin_token
    pin_one(user_id, guid=None)
    await sync_watchlist()
    assert "no plex:// id" in await sync_watchlist()


async def test_a_write_plex_ignores_does_not_cost_you_the_pin(
    client, admin_token, plex
):
    """Plex accepts addToWatchlist and does not always act on it. Believing
    the write turned our own failure into "they removed it over there"."""
    _, user_id = admin_token
    sid = pin_one(user_id)
    plex["accept_writes"] = False

    for _ in range(3):
        await sync_watchlist()
        assert pinned() == {sid}
    assert remembered(sid) == (1, 0)


async def test_an_ignored_write_is_retried_rather_than_abandoned(
    client, admin_token, plex
):
    _, user_id = admin_token
    pin_one(user_id)
    plex["accept_writes"] = False
    await sync_watchlist()
    await sync_watchlist()
    assert len(plex["adds"]) >= 2


async def test_a_write_that_lands_settles_and_stops_retrying(client, admin_token, plex):
    _, user_id = admin_token
    sid = pin_one(user_id)

    assert "+1/-0 watchlist" in await sync_watchlist()
    assert remembered(sid) == (1, 1)

    assert "in step" in await sync_watchlist()
    assert len(plex["adds"]) == 1
    assert pinned() == {sid}


# ── What must still work ──


async def test_removing_it_in_plex_still_unpins_here(client, admin_token, plex):
    """The behaviour the state machine exists for. Breaking the unpin while
    fixing the false unpin would be trading one bug for another."""
    _, user_id = admin_token
    sid = pin_one(user_id)
    await sync_watchlist()
    assert remembered(sid) == (1, 1)

    plex["listed"].clear()
    await sync_watchlist()
    assert pinned() == set()


async def test_adding_it_in_plex_still_pins_here(client, admin_token, plex):
    _, user_id = admin_token
    with session() as conn:
        conn.execute("UPDATE users SET plex_token = 'tok' WHERE id = ?", (user_id,))
        sid = make_series(conn, "Silo", plex_guid=GUID, sonarr_id=7)
    plex["listed"].add(GUID)

    await sync_watchlist()
    assert pinned() == {sid}


async def test_unpinning_here_still_removes_it_from_plex(client, admin_token, plex):
    _, user_id = admin_token
    sid = pin_one(user_id)
    await sync_watchlist()

    with session() as conn:
        conn.execute("DELETE FROM pins WHERE series_id = ?", (sid,))
    await sync_watchlist()
    assert plex["listed"] == set()
