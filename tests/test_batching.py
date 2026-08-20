"""Batched arrival notifications.

A season pack lands as a burst of separate webhooks, one per file. Pushing
from each of them means ten buzzes for a single event, when the useful unit
of news is "there is a season to start".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import save_settings
from app.db import session
from app.jobs.notifications import _summarise, notify_pending
from app.main import app
from tests.factories import iso, make_episode, make_series

SECRET = "batching-secret"


@pytest.fixture
def client(db, admin_token):
    token, user_id = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        save_settings({"webhook_secret": SECRET, "notify_batch_minutes": "5"})
        with session() as conn:
            conn.execute("UPDATE users SET ntfy_topic = 'marc' WHERE id = ?", (user_id,))
        yield c


def seed(conn, user_id, *, episodes, arrived_minutes_ago=10, season=3, title="Silo",
         tvdb_id=12345, sonarr_id=7):
    # tvdb_id is UNIQUE, so a second series in one test needs its own.
    sid = make_series(conn, title, tvdb_id=tvdb_id, sonarr_id=sonarr_id,
                      pinned_by=user_id)
    arrived = iso(hours=-arrived_minutes_ago / 60)
    for number in episodes:
        make_episode(conn, sid, season=season, episode=number,
                     air_date_utc="2026-08-01T20:00:00+00:00", has_file=1,
                     in_plex=0, arrived_at=arrived)
    return sid


# ── Wording ──


def test_a_lone_episode_keeps_the_old_wording():
    title, body = _summarise("Silo", [{"season": 3, "episode": 7, "episode_title": "Radio"}])
    assert title == "Silo S03E07 is in Plex"
    assert body == "Radio"


def test_a_whole_season_reads_as_one_event():
    episodes = [{"season": 3, "episode": n, "episode_title": None} for n in range(1, 11)]
    title, body = _summarise("Silo", episodes)
    assert title == "Silo — Season 3, 10 episodes"
    assert "S03E01" in body
    assert "S03E10" in body


def test_a_mixed_batch_does_not_claim_a_season():
    episodes = [
        {"season": 2, "episode": 9, "episode_title": None},
        {"season": 3, "episode": 1, "episode_title": None},
    ]
    title, _ = _summarise("Silo", episodes)
    assert title == "Silo — 2 episodes just landed"


def test_specials_are_named_rather_than_called_season_zero():
    episodes = [{"season": 0, "episode": n, "episode_title": None} for n in (1, 2)]
    title, _ = _summarise("Taskmaster", episodes)
    assert "Specials" in title


# ── The job ──


async def test_a_settled_batch_becomes_one_push(client, admin_token, pushes):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, episodes=range(1, 11))

    detail = await notify_pending()
    assert len(pushes) == 1
    assert pushes[0]["title"] == "Silo — Season 3, 10 episodes"
    assert "1 notification(s) sent" in detail


async def test_a_batch_still_importing_is_left_alone(client, admin_token, pushes):
    """Ten minutes of importing should still be one notification, not one per
    tick that happened to catch a file mid-flight."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, episodes=[1, 2], arrived_minutes_ago=0)

    detail = await notify_pending()
    assert pushes == []
    assert "still settling" in detail


async def test_it_pushes_once_the_burst_has_settled(client, admin_token, pushes):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id, episodes=[1, 2], arrived_minutes_ago=0)
    await notify_pending()
    assert pushes == []

    settled = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    with session() as conn:
        conn.execute("UPDATE episodes SET arrived_at = ? WHERE series_id = ?", (settled, sid))
    await notify_pending()
    assert len(pushes) == 1


async def test_nothing_is_sent_twice(client, admin_token, pushes):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, episodes=[1, 2])

    await notify_pending()
    await notify_pending()
    assert len(pushes) == 1


async def test_two_series_landing_together_are_separate_pushes(client, admin_token, pushes):
    """Grouping is by series, not by moment — one notification per thing you
    might sit down and watch."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, episodes=[1, 2])
        seed(conn, user_id, episodes=[1], title="Andor", tvdb_id=999, sonarr_id=8)

    await notify_pending()
    assert len(pushes) == 2


async def test_turning_batching_off_hands_the_push_back_to_the_webhook(client, admin_token,
                                                                      pushes):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, episodes=[1])
    save_settings({"notify_batch_minutes": "0"})

    assert await notify_pending() == "nothing pending"
    assert pushes == []


async def test_a_season_pack_pin_is_still_this_jobs_work_without_batching(
    client, admin_token, pushes
):
    """The webhook pushes directly when batching is off, but it deliberately
    skips season-pack pins — so if this job stood down entirely, those pins
    would never be notified at all."""
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id, episodes=[1])
        conn.execute("UPDATE pins SET season_pack = 1 WHERE series_id = ?", (sid,))
    save_settings({"notify_batch_minutes": "0"})

    await notify_pending()
    assert len(pushes) == 1


def test_the_webhook_queues_instead_of_pushing_while_batching(client, admin_token, pushes):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, episodes=[1])

    payload = {
        "eventType": "Download",
        "series": {"id": 7, "title": "Silo", "tvdbId": 12345},
        "episodes": [{"seasonNumber": 3, "episodeNumber": 1}],
    }
    r = client.post(f"/hooks/sonarr?secret={SECRET}", json=payload)
    assert "queued for batching" in r.json()["detail"]
    assert pushes == []
