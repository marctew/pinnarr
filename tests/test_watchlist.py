"""Two-way sync between pins and the Plex Watchlist.

The watchlist lives in Plex's cloud and belongs to an account, so each user
carries their own token. A watchlist entry can only become a pin if the show
is already in the library — otherwise there is nothing to pin.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import auth
from app.clients.watchlist import DISCOVER, rating_key_from_guid
from app.config import save_settings
from app.db import session, utcnow
from app.jobs.watchlist_sync import sync_watchlist
from app.main import app
from tests.factories import make_series

GUID = "plex://show/5d9c0874ffd9ef001e99607a"
KEY = "5d9c0874ffd9ef001e99607a"


@pytest.fixture
def client(db, admin_token):
    token, user_id = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        save_settings({"plex_watchlist_sync": "true"})
        with session() as conn:
            auth.set_plex_token(conn, user_id, "plex-token")
        yield c


def add(conn, title="Silo", *, guid=GUID, pinned_by=None):
    return make_series(conn, title, plex_guid=guid, pinned_by=pinned_by)


def remember(conn, user_id, series_id, *, pinned, listed):
    conn.execute(
        "INSERT INTO watchlist_sync_state (user_id, series_id, pinned, listed, synced_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, series_id, int(pinned), int(listed), utcnow()),
    )


def mock_plex(items=()):
    respx.get(f"{DISCOVER}/library/sections/watchlist/all").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": list(items)}})
    )
    return respx.put(url__regex=rf"{DISCOVER}/actions/.*").mock(
        return_value=httpx.Response(200, json={})
    )


def show(guid=GUID, key=KEY, title="Silo"):
    return {"type": "show", "guid": guid, "ratingKey": key, "title": title}


def is_pinned(conn, user_id, series_id) -> bool:
    return conn.execute(
        "SELECT 1 FROM pins WHERE user_id = ? AND series_id = ?", (user_id, series_id)
    ).fetchone() is not None


# ── Identity ──


def test_the_discover_key_is_the_tail_of_the_plex_guid():
    """A show already matched in Plex needs no lookup to be watchlisted."""
    assert rating_key_from_guid(GUID) == KEY


@pytest.mark.parametrize("guid", [None, "", "tvdb://12345", "not a guid"])
def test_anything_that_is_not_a_plex_guid_yields_nothing(guid):
    assert rating_key_from_guid(guid) is None


# ── Outward ──


@respx.mock
async def test_a_pin_is_added_to_the_watchlist(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        add(conn, pinned_by=user_id)
    actions = mock_plex()

    detail = await sync_watchlist()
    assert "+1/-0 watchlist" in detail
    assert "addToWatchlist" in str(actions.calls[0].request.url)
    assert KEY in str(actions.calls[0].request.url)


@respx.mock
async def test_unpinning_here_removes_it_from_the_watchlist(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn)
        remember(conn, user_id, sid, pinned=True, listed=True)
    actions = mock_plex([show()])

    detail = await sync_watchlist()
    assert "-1 watchlist" in detail
    assert "removeFromWatchlist" in str(actions.calls[0].request.url)


@respx.mock
async def test_a_series_plex_never_matched_cannot_be_watchlisted(client, admin_token):
    """No plex:// identity means nothing in Discover corresponds to it."""
    _, user_id = admin_token
    with session() as conn:
        add(conn, guid=None, pinned_by=user_id)
    actions = mock_plex()

    assert "in step" in await sync_watchlist()
    assert actions.call_count == 0


# ── Inward ──


@respx.mock
async def test_watchlisting_in_plex_pins_it_here(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn)
    mock_plex([show()])

    detail = await sync_watchlist()
    assert "+1/-0 pins" in detail
    with session() as conn:
        assert is_pinned(conn, user_id, sid)


@respx.mock
async def test_removing_it_from_the_watchlist_unpins_it_here(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, pinned_by=user_id)
        remember(conn, user_id, sid, pinned=True, listed=True)
    mock_plex([])

    detail = await sync_watchlist()
    assert "-1 pins" in detail
    with session() as conn:
        assert not is_pinned(conn, user_id, sid)


@respx.mock
async def test_a_watchlisted_show_you_do_not_own_is_reported_not_invented(client):
    """Pinning it would make a series with no episodes, no Sonarr entry and
    no poster."""
    mock_plex([show(guid="plex://show/somethingelse", key="other", title="Unowned")])
    assert "1 watchlisted show(s) not in your library" in await sync_watchlist()


@respx.mock
async def test_films_are_ignored(client):
    mock_plex([{"type": "movie", "guid": "plex://movie/abc", "ratingKey": "abc",
                "title": "Dune"}])
    detail = await sync_watchlist()
    assert "not in your library" not in detail


# ── Direction ──


@respx.mock
async def test_pinnarr_wins_when_both_sides_moved(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, pinned_by=user_id)
        remember(conn, user_id, sid, pinned=False, listed=True)
    actions = mock_plex([])

    await sync_watchlist()
    assert "addToWatchlist" in str(actions.calls[0].request.url)
    with session() as conn:
        assert is_pinned(conn, user_id, sid)


@respx.mock
async def test_a_pair_in_step_does_nothing(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, pinned_by=user_id)
        remember(conn, user_id, sid, pinned=True, listed=True)
    actions = mock_plex([show()])

    assert "in step" in await sync_watchlist()
    assert actions.call_count == 0


@respx.mock
async def test_running_twice_changes_nothing_the_second_time(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        add(conn, pinned_by=user_id)
    mock_plex()

    await sync_watchlist()
    respx.get(f"{DISCOVER}/library/sections/watchlist/all").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": [show()]}})
    )
    assert "in step" in await sync_watchlist()


# ── Guards ──


async def test_it_stays_off_until_asked(db, admin_token):
    save_settings({"plex_watchlist_sync": "false"})
    assert "watchlist sync is off" in await sync_watchlist()


async def test_a_user_without_a_token_is_skipped(db, admin_token):
    save_settings({"plex_watchlist_sync": "true"})
    assert "nobody has a Plex token" in await sync_watchlist()


@respx.mock
async def test_plex_being_unreachable_is_reported_not_raised(client):
    respx.get(f"{DISCOVER}/library/sections/watchlist/all").mock(
        side_effect=httpx.ConnectError("down")
    )
    assert "admin:" in await sync_watchlist()


# ── The profile ──


def test_the_token_is_never_rendered_back(client, admin_token):
    body = client.get("/profile").text
    assert "plex-token" not in body
    assert "saved — leave blank to keep" in body


def test_an_empty_box_keeps_the_stored_token(client, admin_token):
    _, user_id = admin_token
    client.post("/profile", data={"ntfy_topic": "marc", "plex_token": ""})
    with session() as conn:
        assert auth.get_user(conn, user_id)["plex_token"] == "plex-token"


def test_a_new_token_replaces_it(client, admin_token):
    _, user_id = admin_token
    client.post("/profile", data={"ntfy_topic": "", "plex_token": "fresh"})
    with session() as conn:
        assert auth.get_user(conn, user_id)["plex_token"] == "fresh"


@respx.mock
def test_the_test_button_reports_what_it_found(client):
    mock_plex([show()])
    body = client.post("/api/profile/watchlist-test").json()
    assert body["ok"] is True
    assert "1 TV show" in body["message"]


@respx.mock
async def test_pins_with_no_plex_identity_are_counted_not_skipped_quietly(client, admin_token):
    """plex_guid arrives with a Plex sync. Straight after an upgrade every
    series lacks one, and reporting "in step" would be a lie."""
    _, user_id = admin_token
    with session() as conn:
        add(conn, guid=None, pinned_by=user_id)
    mock_plex()

    detail = await sync_watchlist()
    assert "no plex:// id yet" in detail
    assert "run the plex_library job" in detail
