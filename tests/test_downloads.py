"""The download queue, and telling a slow grab from a stuck one."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import session, utcnow
from app.main import app
from app.repo import STALLED_HOURS, downloads
from tests.factories import iso, make_episode, make_series


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


def queued(conn, sonarr_episode_id, *, percent=40.0, status="downloading",
           time_left="00:12:00", moved_hours_ago=0.1, message=None):
    conn.execute(
        "INSERT INTO download_queue (sonarr_episode_id, status, percent, time_left, "
        "message, first_seen_at, progress_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (sonarr_episode_id, status, percent, time_left, message,
         iso(hours=-24), iso(hours=-moved_hours_ago), utcnow()),
    )


def seed(conn, user_id, *, sonarr_episode_id=555, **queue):
    sid = make_series(conn, "Silo", pinned_by=user_id)
    make_episode(conn, sid, season=3, episode=8, title="Radio",
                 sonarr_episode_id=sonarr_episode_id, air_date_utc=iso(days=-1))
    queued(conn, sonarr_episode_id, **queue)
    return sid


# ── The page ──


def test_an_active_download_is_listed(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    body = client.get("/downloads").text
    assert "Silo" in body
    assert "S03E08" in body
    assert "40%" in body


def test_an_empty_queue_says_so(client):
    assert "isn't downloading anything" in client.get("/downloads").text


def test_everything_sonarr_is_doing_is_listed(db, account):
    """The page answers "what is Sonarr doing" as well as "what is coming for
    me". It used to answer only the second, and silently."""
    _, marc = account()
    _, bob = account("bob", "user")
    with session() as conn:
        seed(conn, marc)
        rows = downloads(conn, bob)
    assert len(rows) == 1
    assert rows[0]["is_pinned"] == 0


def test_your_pins_come_first(db, admin_token):
    """Sonarr fetches for its whole library, and most of it is not yours."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, sonarr_episode_id=555)
        other = make_series(conn, "Not Yours")
        make_episode(conn, other, sonarr_episode_id=999)
        queued(conn, 999, percent=95.0)
        rows = downloads(conn, user_id)

    assert [r["sonarr_episode_id"] for r in rows] == [555, 999]
    assert [r["is_pinned"] for r in rows] == [1, 0]


def test_a_download_pinnarr_has_no_episode_row_for_is_still_listed(db, admin_token):
    """The calendar syncs a window. Sonarr will happily fetch season two of
    something from 2016, and dropping it would be a second invisible filter
    on a page that claims to show everything."""
    _, user_id = admin_token
    with session() as conn:
        conn.execute(
            "INSERT INTO download_queue (sonarr_episode_id, status, percent, "
            "series_title, episode_title, season, episode, first_seen_at, "
            "progress_at, updated_at) VALUES (777, 'downloading', 20, "
            "'Some Old Show', 'The One With The Thing', 2, 4, ?, ?, ?)",
            (iso(hours=-2), iso(hours=-1), utcnow()),
        )
        rows = downloads(conn, user_id)

    assert len(rows) == 1
    assert rows[0]["series_title"] == "Some Old Show"
    assert rows[0]["episode_title"] == "The One With The Thing"
    assert rows[0]["season"] == 2
    assert rows[0]["series_id"] is None


def test_a_stalled_pin_still_outranks_a_healthy_one(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo", pinned_by=user_id)
        make_episode(conn, sid, season=1, episode=1, sonarr_episode_id=1)
        make_episode(conn, sid, season=1, episode=2, sonarr_episode_id=2)
        queued(conn, 1, percent=90.0, moved_hours_ago=0.1)
        queued(conn, 2, percent=3.0, moved_hours_ago=STALLED_HOURS + 2)
        rows = downloads(conn, user_id)
    assert [r["sonarr_episode_id"] for r in rows] == [2, 1]


# ── Stalling ──


def test_a_download_that_has_not_moved_is_flagged(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, moved_hours_ago=STALLED_HOURS + 1)
        assert downloads(conn, user_id)[0]["stalled"] == 1


def test_a_download_that_moved_recently_is_not(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, moved_hours_ago=STALLED_HOURS - 1)
        assert downloads(conn, user_id)[0]["stalled"] == 0


def test_a_finished_item_awaiting_import_is_not_stalled(db, admin_token):
    """100% and sitting there is Sonarr importing, not a dead grab."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, percent=100.0, moved_hours_ago=STALLED_HOURS + 5)
        assert downloads(conn, user_id)[0]["stalled"] == 0


def test_stalled_items_sort_above_healthy_ones(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo", pinned_by=user_id)
        make_episode(conn, sid, season=1, episode=1, sonarr_episode_id=1)
        make_episode(conn, sid, season=1, episode=2, sonarr_episode_id=2)
        queued(conn, 1, percent=90.0, moved_hours_ago=0.1)
        queued(conn, 2, percent=3.0, moved_hours_ago=STALLED_HOURS + 2)
        rows = downloads(conn, user_id)
    assert [r["sonarr_episode_id"] for r in rows] == [2, 1]


def test_the_page_explains_what_stalled_means(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, moved_hours_ago=STALLED_HOURS + 1)
    body = client.get("/downloads").text
    assert "stalled" in body
    assert f"{STALLED_HOURS} hours" in body


def test_a_queue_message_is_shown(client, admin_token):
    """Sonarr's own words about why something is not moving."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, message="The download is stalled with no connections")
    assert "no connections" in client.get("/downloads").text


# ── Keeping a history across ticks ──


@pytest.fixture
def sonarr_queue(monkeypatch):
    """Whatever the next sync_queue() run should see."""
    from app.clients.sonarr import QueueItem, SonarrClient

    state: list = []

    async def fake_queue(_self):
        return list(state)

    monkeypatch.setattr(SonarrClient, "queue", fake_queue)

    def set_queue(*items):
        state[:] = [
            QueueItem(sonarr_episode_id=i, sonarr_series_id=7, status="downloading",
                      percent=p, time_left="00:10:00", message=None)
            for i, p in items
        ]

    return set_queue


async def run_sync(client):
    from app.config import save_settings
    from app.jobs.queue_sync import sync_queue

    save_settings({"sonarr_url": "http://sonarr.lan:8989", "sonarr_api_key": "k"})
    return await sync_queue()


async def test_progress_is_only_stamped_when_the_percentage_moves(client, sonarr_queue):
    """Sonarr rewrites the row every poll, so updated_at moves whether or not
    the download does. Only percent is evidence."""
    sonarr_queue((555, 40.0))
    await run_sync(client)
    # Backdated so the two outcomes are distinguishable: utcnow() has second
    # resolution and three ticks in a test land inside one.
    stale = iso(hours=-5)
    with session() as conn:
        conn.execute("UPDATE download_queue SET progress_at = ?", (stale,))

    await run_sync(client)
    with session() as conn:
        assert conn.execute("SELECT progress_at FROM download_queue").fetchone()[0] == stale

    sonarr_queue((555, 41.0))
    await run_sync(client)
    with session() as conn:
        assert conn.execute("SELECT progress_at FROM download_queue").fetchone()[0] > stale


async def test_first_seen_survives_later_ticks(client, sonarr_queue):
    sonarr_queue((555, 10.0))
    await run_sync(client)
    with session() as conn:
        started = conn.execute("SELECT first_seen_at FROM download_queue").fetchone()[0]

    sonarr_queue((555, 55.0))
    await run_sync(client)
    with session() as conn:
        assert conn.execute("SELECT first_seen_at FROM download_queue").fetchone()[0] == started


async def test_an_item_that_leaves_the_queue_is_dropped(client, sonarr_queue):
    """Gone means finished or failed, and either way the progress is a lie."""
    sonarr_queue((555, 90.0), (556, 20.0))
    await run_sync(client)
    sonarr_queue((556, 30.0))
    await run_sync(client)
    with session() as conn:
        ids = [r[0] for r in conn.execute("SELECT sonarr_episode_id FROM download_queue")]
    assert ids == [556]


async def test_a_returning_item_starts_its_clock_again(client, sonarr_queue):
    """A failed grab that Sonarr retries is a new download, not a three-hour
    stall inherited from the last attempt."""
    sonarr_queue((555, 90.0))
    await run_sync(client)
    sonarr_queue()
    await run_sync(client)
    sonarr_queue((555, 5.0))
    await run_sync(client)
    with session() as conn:
        row = conn.execute(
            "SELECT first_seen_at, progress_at FROM download_queue"
        ).fetchone()
    assert row["first_seen_at"] == row["progress_at"]


async def test_a_row_from_before_the_columns_existed_gets_a_clock(client, sonarr_queue):
    """After the upgrade, existing queue rows have no progress stamp. Leaving
    it null would make a genuinely stuck download permanently unflaggable:
    the only thing that would stamp it is the movement it will never make."""
    with session() as conn:
        conn.execute(
            "INSERT INTO download_queue (sonarr_episode_id, status, percent, updated_at) "
            "VALUES (555, 'downloading', 42.0, ?)", (utcnow(),),
        )
    sonarr_queue((555, 42.0))
    await run_sync(client)

    with session() as conn:
        row = conn.execute(
            "SELECT first_seen_at, progress_at FROM download_queue"
        ).fetchone()
    assert row["progress_at"] is not None
    assert row["first_seen_at"] is not None


def test_the_api_reports_whether_each_item_is_yours(client, admin_token):
    """The queue is now everyone's, so a caller needs to be told which rows
    are the ones it was asking about."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, sonarr_episode_id=555)
        other = make_series(conn, "Not Yours")
        make_episode(conn, other, sonarr_episode_id=999)
        queued(conn, 999)

    body = client.get("/api/downloads").json()
    assert body["mine"] == 1
    assert [i["pinned"] for i in body["items"]] == [True, False]


def test_the_page_marks_which_are_yours(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, sonarr_episode_id=555)
        other = make_series(conn, "Not Yours")
        make_episode(conn, other, sonarr_episode_id=999)
        queued(conn, 999)
    body = client.get("/downloads").text
    assert "dl mine" in body
    assert "1 of 2" in body
