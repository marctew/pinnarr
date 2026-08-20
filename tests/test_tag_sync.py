"""Two-way sync between pins and Sonarr tags.

The tests that matter are about direction. "Pinned here but not tagged there"
is ambiguous — it means either *tag it in Sonarr* or *unpin it here* — and
guessing wrong silently undoes whatever someone just did.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import auth
from app.config import save_settings
from app.db import session, utcnow
from app.jobs.tag_sync import sync_tags, tag_label
from app.main import app

SONARR = "http://sonarr.lan:8989"
TAG_ID = 7


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        save_settings({
            "sonarr_url": SONARR, "sonarr_api_key": "key", "sonarr_tag_sync": "true",
        })
        yield c


def add(conn, title="Silo", *, sonarr_id=101, pinned_by=None):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, sonarr_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (title, title.lower(), sonarr_id, now, now),
    )
    sid = int(cur.lastrowid)
    if pinned_by:
        conn.execute(
            "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
            (pinned_by, sid, now),
        )
        conn.execute("UPDATE series SET pinned = 1 WHERE id = ?", (sid,))
    return sid


def remember(conn, user_id, series_id, *, pinned, tagged):
    conn.execute(
        "INSERT INTO tag_sync_state (user_id, series_id, pinned, tagged, synced_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, series_id, int(pinned), int(tagged), utcnow()),
    )


def mock_sonarr(*, tags=None, series=None):
    respx.get(f"{SONARR}/api/v3/tag").mock(
        return_value=httpx.Response(200, json=tags if tags is not None
                                    else [{"id": TAG_ID, "label": "pinnarr-admin"}])
    )
    respx.get(f"{SONARR}/api/v3/series").mock(
        return_value=httpx.Response(200, json=series or [])
    )
    return respx.put(f"{SONARR}/api/v3/series/editor").mock(
        return_value=httpx.Response(202, json={})
    )


def is_pinned(conn, user_id, series_id) -> bool:
    return conn.execute(
        "SELECT 1 FROM pins WHERE user_id = ? AND series_id = ?", (user_id, series_id)
    ).fetchone() is not None


# ── Naming ──


def test_the_tag_is_named_after_the_account():
    assert tag_label("marc") == "pinnarr-marc"


def test_awkward_usernames_are_normalised():
    """Sonarr lowercases tags and dislikes spaces."""
    assert tag_label("Marc Tew") == "pinnarr-marc-tew"
    assert tag_label("  ") == "pinnarr-user"


# ── Outward ──


@respx.mock
async def test_a_new_pin_is_tagged_in_sonarr(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        add(conn, pinned_by=user_id)
    editor = mock_sonarr()

    detail = await sync_tags()
    assert "+1/-0 tags" in detail
    body = editor.calls[0].request.read().decode()
    assert '"applyTags":"add"' in body
    assert '"seriesIds":[101]' in body


@respx.mock
async def test_unpinning_here_removes_the_tag(client, admin_token):
    """Previously pinned and tagged; the pin has gone, so the tag should."""
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn)
        remember(conn, user_id, sid, pinned=True, tagged=True)
    editor = mock_sonarr(series=[{"id": 101, "tags": [TAG_ID]}])

    detail = await sync_tags()
    assert "-1 tags" in detail
    assert '"applyTags":"remove"' in editor.calls[0].request.read().decode()


@respx.mock
async def test_the_tag_is_created_on_demand(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        add(conn, pinned_by=user_id)
    respx.post(f"{SONARR}/api/v3/tag").mock(
        return_value=httpx.Response(201, json={"id": 99, "label": "pinnarr-admin"})
    )
    mock_sonarr(tags=[])

    await sync_tags()
    assert respx.calls.call_count >= 3


@respx.mock
async def test_no_pins_and_no_tag_makes_no_calls_to_change_anything(client):
    mock_sonarr(tags=[])
    assert "nothing to mirror" in await sync_tags()


# ── Inward ──


@respx.mock
async def test_tagging_in_sonarr_pins_it_here(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn)
    mock_sonarr(series=[{"id": 101, "tags": [TAG_ID]}])

    detail = await sync_tags()
    assert "+1/-0 pins" in detail
    with session() as conn:
        assert is_pinned(conn, user_id, sid)


@respx.mock
async def test_removing_the_tag_in_sonarr_unpins_it_here(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, pinned_by=user_id)
        remember(conn, user_id, sid, pinned=True, tagged=True)
    mock_sonarr(series=[{"id": 101, "tags": []}])

    detail = await sync_tags()
    assert "-1 pins" in detail
    with session() as conn:
        assert not is_pinned(conn, user_id, sid)


# ── Direction ──


@respx.mock
async def test_pinnarr_wins_when_both_sides_moved(client, admin_token):
    """Pinned here and untagged there since the last sync. Pinning is a
    deliberate act here and a side effect of housekeeping there."""
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, pinned_by=user_id)
        remember(conn, user_id, sid, pinned=False, tagged=True)
    editor = mock_sonarr(series=[{"id": 101, "tags": []}])

    await sync_tags()
    assert '"applyTags":"add"' in editor.calls[0].request.read().decode()
    with session() as conn:
        assert is_pinned(conn, user_id, sid)


@respx.mock
async def test_a_pair_already_in_step_does_nothing(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, pinned_by=user_id)
        remember(conn, user_id, sid, pinned=True, tagged=True)
    editor = mock_sonarr(series=[{"id": 101, "tags": [TAG_ID]}])

    assert "in step" in await sync_tags()
    assert editor.call_count == 0


@respx.mock
async def test_running_twice_changes_nothing_the_second_time(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        add(conn, pinned_by=user_id)
    mock_sonarr()

    await sync_tags()
    # Sonarr now reflects the pin, as it would on the next run.
    respx.get(f"{SONARR}/api/v3/series").mock(
        return_value=httpx.Response(200, json=[{"id": 101, "tags": [TAG_ID]}])
    )
    assert "in step" in await sync_tags()


@respx.mock
async def test_each_account_gets_its_own_tag(client, admin_token, account):
    _, admin_id = admin_token
    _, bob_id = account("bob", "user")
    with session() as conn:
        add(conn, pinned_by=admin_id)
    created = respx.post(f"{SONARR}/api/v3/tag").mock(
        return_value=httpx.Response(201, json={"id": 8, "label": "pinnarr-bob"})
    )
    mock_sonarr()

    detail = await sync_tags()
    assert "admin:" in detail
    assert "bob:" in detail
    # bob has no pins, so no tag is created for him yet.
    assert created.call_count == 0


# ── Guards ──


async def test_it_stays_off_until_asked(db, admin_token):
    save_settings({"sonarr_url": SONARR, "sonarr_api_key": "key", "sonarr_tag_sync": "false"})
    assert "tag sync is off" in await sync_tags()


async def test_without_sonarr_it_does_nothing(db, admin_token):
    save_settings({"sonarr_url": "", "sonarr_api_key": "", "sonarr_tag_sync": "true"})
    assert "Sonarr not configured" in await sync_tags()


@respx.mock
async def test_an_unreachable_sonarr_is_reported_not_raised(client):
    respx.get(f"{SONARR}/api/v3/tag").mock(side_effect=httpx.ConnectError("down"))
    assert "error" in await sync_tags()


@respx.mock
async def test_the_library_is_fetched_once_however_many_accounts(client, admin_token, account):
    """The response is the entire library. Multiplying it by the number of
    accounts every ten minutes would be rude to a service on the same box."""
    _, admin_id = admin_token
    account("bob", "user")
    account("kate", "user")
    with session() as conn:
        add(conn, pinned_by=admin_id)
    mock_sonarr()

    await sync_tags()
    series_calls = [c for c in respx.calls if c.request.url.path == "/api/v3/series"]
    assert len(series_calls) == 1
