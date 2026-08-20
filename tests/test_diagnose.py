"""Download progress, diagnosis, and asking Sonarr to search again.

Together these answer the question the red row raises: is it coming, is it
stuck, or has Sonarr given up? Until now it could only assert that something
was wrong.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import auth
from app.config import save_settings
from app.db import session, utcnow
from app.diagnose import _summarise
from app.episodes import episode_state
from app.jobs.queue_sync import sync_queue
from app.main import app

SONARR = "http://sonarr.lan:8989"


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        save_settings({"sonarr_url": SONARR, "sonarr_api_key": "key"})
        yield c


def seed(conn, *, sonarr_episode_id=555):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, pinned, created_at, updated_at) "
        "VALUES ('Silo', 'silo', 1, ?, ?)",
        (now, now),
    )
    sid = int(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO episodes (series_id, sonarr_episode_id, season, episode, title, "
        "air_date_utc, has_file, in_plex, monitored, updated_at) "
        "VALUES (?, ?, 3, 8, 'Radio', '2026-08-10T20:00:00+00:00', 0, 0, 1, ?)",
        (sid, sonarr_episode_id, now),
    )
    return sid, int(cur.lastrowid)


# ── The state ──


def test_something_downloading_is_neither_expected_nor_missing():
    """Red would be a lie and amber an understatement."""
    row = {
        "has_file": 0, "in_plex": 0, "monitored": 1,
        "air_date_utc": "2020-01-01T00:00:00+00:00",
        "dl_status": "downloading", "dl_percent": 62.0,
    }
    assert episode_state(row) == "downloading"


def test_without_a_queue_entry_it_is_still_missing():
    row = {
        "has_file": 0, "in_plex": 0, "monitored": 1,
        "air_date_utc": "2020-01-01T00:00:00+00:00",
        "dl_status": None, "dl_percent": None,
    }
    assert episode_state(row) == "missing"


# ── The queue sync ──


@respx.mock
async def test_the_queue_is_stored(client):
    respx.get(f"{SONARR}/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": [
            {"episodeId": 555, "seriesId": 7, "size": 1000, "sizeleft": 250,
             "trackedDownloadState": "downloading", "timeleft": "00:12:00"},
        ]})
    )
    detail = await sync_queue()
    assert "1 item(s)" in detail
    with session() as conn:
        row = conn.execute("SELECT * FROM download_queue").fetchone()
    assert row["sonarr_episode_id"] == 555
    assert round(row["percent"]) == 75


@respx.mock
async def test_leaving_the_queue_clears_the_progress(client):
    """Stale progress is worse than none — it says something is happening
    when it has either finished or failed."""
    respx.get(f"{SONARR}/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": [
            {"episodeId": 555, "seriesId": 7, "size": 100, "sizeleft": 50},
        ]})
    )
    await sync_queue()

    respx.get(f"{SONARR}/api/v3/queue").mock(return_value=httpx.Response(200, json={"records": []}))
    await sync_queue()
    with session() as conn:
        assert conn.execute("SELECT count(*) AS n FROM download_queue").fetchone()["n"] == 0


@respx.mock
async def test_a_record_without_an_episode_is_skipped(client):
    respx.get(f"{SONARR}/api/v3/queue").mock(
        return_value=httpx.Response(200, json={"records": [{"seriesId": 7}, "junk"]})
    )
    assert "0 item(s)" in await sync_queue()


# ── The explanation ──


def test_nothing_found_says_so():
    assert "nothing has ever" in _summarise([], None)


def test_a_failed_download_is_distinguished_from_never_found():
    assert "failed" in _summarise([{"eventType": "downloadFailed"}], None)


def test_an_import_points_the_finger_at_plex():
    """Sonarr has done its job; the gap is downstream."""
    assert "Plex" in _summarise([{"eventType": "downloadFolderImported"}], None)


def test_a_live_download_outranks_any_history():
    queued = {"percent": 62.0, "time_left": "00:10:00", "status": "downloading"}
    assert "62%" in _summarise([{"eventType": "downloadFailed"}], queued)


@respx.mock
def test_the_endpoint_explains_a_row(client):
    respx.get(f"{SONARR}/api/v3/history").mock(
        return_value=httpx.Response(200, json={"records": []})
    )
    with session() as conn:
        _, eid = seed(conn)
    body = client.post(f"/api/episodes/{eid}/why").json()
    assert body["ok"] is True
    assert "nothing has ever" in body["detail"]


def test_an_episode_sonarr_does_not_know_is_explained_not_500ed(client):
    with session() as conn:
        _, eid = seed(conn, sonarr_episode_id=None)
    body = client.post(f"/api/episodes/{eid}/why").json()
    assert body["ok"] is False
    assert "doesn't track" in body["detail"]


@respx.mock
def test_an_unreachable_sonarr_is_reported_not_raised(client):
    respx.get(f"{SONARR}/api/v3/history").mock(side_effect=httpx.ConnectError("down"))
    with session() as conn:
        _, eid = seed(conn)
    assert client.post(f"/api/episodes/{eid}/why").json()["ok"] is False


# ── Searching again ──


@respx.mock
def test_search_asks_sonarr_to_look(client):
    route = respx.post(f"{SONARR}/api/v3/command").mock(
        return_value=httpx.Response(201, json={"id": 42})
    )
    with session() as conn:
        _, eid = seed(conn)

    body = client.post(f"/api/episodes/{eid}/search").json()
    assert body["command"] == 42
    assert route.calls[0].request.read() == b'{"name":"EpisodeSearch","episodeIds":[555]}'


def test_searching_an_unknown_episode_is_a_404(client):
    assert client.post("/api/episodes/99999/search").status_code == 404


def test_searching_something_sonarr_does_not_track_is_a_409(client):
    with session() as conn:
        _, eid = seed(conn, sonarr_episode_id=None)
    assert client.post(f"/api/episodes/{eid}/search").status_code == 409


@respx.mock
def test_a_sonarr_failure_surfaces_as_a_bad_gateway(client):
    respx.post(f"{SONARR}/api/v3/command").mock(side_effect=httpx.ConnectError("down"))
    with session() as conn:
        _, eid = seed(conn)
    assert client.post(f"/api/episodes/{eid}/search").status_code == 502
