"""Per-episode watch state.

Tautulli only ever gave us one timestamp per series, used for library
sorting, so marking something watched in Plex changed a sort order and
nothing else — Ready to Watch could never shrink.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import auth
from app.clients.tautulli import TautulliClient
from app.config import save_settings
from app.db import session, utcnow
from app.jobs.availability import sync_availability
from app.main import app
from app.repo import arrival_is_plausible, mark_watched

TAUTULLI = "http://tautulli.lan:8181"


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        save_settings({"tautulli_url": TAUTULLI, "tautulli_api_key": "key"})
        yield c


def seed(conn, user_id, *, plex_key="9001", episodes=(1, 2, 3), days_ago=2):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, plex_rating_key, pinned, "
        "created_at, updated_at) VALUES ('Silo', 'silo', ?, 1, ?, ?)",
        (plex_key, now, now),
    )
    sid = int(cur.lastrowid)
    aired = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    for number in episodes:
        conn.execute(
            "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
            "has_file, in_plex, monitored, runtime, updated_at) "
            "VALUES (?, 1, ?, ?, ?, 1, 1, 1, 45, ?)",
            (sid, number, f"Episode {number}", aired, now),
        )
    conn.execute(
        "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
        (user_id, sid, now),
    )
    return sid


# ── Marking ──


def test_marking_one_episode_leaves_the_others(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        assert mark_watched(conn, "9001", 1, 2, utcnow()) is True
        watched = conn.execute(
            "SELECT episode FROM episodes WHERE watched_at IS NOT NULL"
        ).fetchall()
    assert [r["episode"] for r in watched] == [2]


def test_an_episode_we_do_not_hold_is_reported_not_invented(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        assert mark_watched(conn, "9001", 9, 9, utcnow()) is False


def test_the_earliest_play_is_kept(db, admin_token):
    """A rewatch is not when you first saw it."""
    _, user_id = admin_token
    first = "2026-01-01T20:00:00+00:00"
    second = "2026-08-01T20:00:00+00:00"
    with session() as conn:
        seed(conn, user_id)
        mark_watched(conn, "9001", 1, 1, second)
        mark_watched(conn, "9001", 1, 1, first)
        got = conn.execute(
            "SELECT watched_at FROM episodes WHERE season = 1 AND episode = 1"
        ).fetchone()["watched_at"]
    assert got == first


# ── Ready shrinks ──


def test_a_watched_episode_drops_off_ready(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    assert client.get("/ready").text.count("Episode ") == 3

    with session() as conn:
        mark_watched(conn, "9001", 1, 2, utcnow())
    body = client.get("/ready").text
    assert "Episode 2" not in body
    assert "Episode 1" in body


def test_watching_everything_empties_the_page(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        for number in (1, 2, 3):
            mark_watched(conn, "9001", 1, number, utcnow())
    assert "Either you're caught up" in client.get("/ready").text


# ── The Tautulli feed ──


@respx.mock
async def test_only_completed_plays_count(client):
    """A show you gave up on twenty minutes in should stay on the list."""
    respx.get(f"{TAUTULLI}/api/v2").mock(
        return_value=httpx.Response(200, json={"response": {"result": "success", "data": {
            "data": [
                {"grandparent_rating_key": "9001", "parent_media_index": 1,
                 "media_index": 1, "watched_status": 1, "stopped": 1755000000},
                {"grandparent_rating_key": "9001", "parent_media_index": 1,
                 "media_index": 2, "watched_status": 0.5, "stopped": 1755000000},
            ]
        }}})
    )
    plays = await TautulliClient().watched_episodes()
    assert [p.episode for p in plays] == [1]


@respx.mock
async def test_malformed_history_rows_are_skipped(client):
    respx.get(f"{TAUTULLI}/api/v2").mock(
        return_value=httpx.Response(200, json={"response": {"result": "success", "data": {
            "data": [
                "junk",
                {"grandparent_rating_key": "", "watched_status": 1},
                {"grandparent_rating_key": "9001", "parent_media_index": None,
                 "media_index": 3, "watched_status": 1, "stopped": 1755000000},
                {"grandparent_rating_key": "9001", "parent_media_index": 1,
                 "media_index": 4, "watched_status": 1, "stopped": 1755000000},
            ]
        }}})
    )
    plays = await TautulliClient().watched_episodes()
    assert [p.episode for p in plays] == [4]


# ── Availability no longer invents arrivals ──


def test_a_back_catalogue_appearing_in_plex_is_not_an_arrival():
    """Seeing a 2020 episode for the first time is Pinnarr looking, not the
    episode landing."""
    assert arrival_is_plausible("2020-05-01T20:00:00+00:00") is False


def test_something_that_aired_yesterday_still_counts():
    yesterday = (datetime.now(UTC) - timedelta(hours=20)).isoformat()
    assert arrival_is_plausible(yesterday) is True


@respx.mock
async def test_the_availability_job_does_not_backdate_a_back_catalogue(db, admin_token,
                                                                      monkeypatch):
    _, user_id = admin_token
    save_settings({"plex_url": "http://plex.lan:32400", "plex_token": "t"})
    with session() as conn:
        seed(conn, user_id, days_ago=900)
        conn.execute("UPDATE episodes SET in_plex = 0, arrived_at = NULL")

    async def present(_self, _key):
        return {(1, 1), (1, 2), (1, 3)}

    from app.clients.plex import PlexClient

    monkeypatch.setattr(PlexClient, "episode_keys_present", present)
    await sync_availability()

    with session() as conn:
        stamped = conn.execute(
            "SELECT count(*) AS n FROM episodes WHERE arrived_at IS NOT NULL"
        ).fetchone()["n"]
        seen = conn.execute(
            "SELECT count(*) AS n FROM episodes WHERE in_plex = 1"
        ).fetchone()["n"]
    assert seen == 3
    assert stamped == 0
