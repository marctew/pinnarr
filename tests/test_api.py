"""The half of the API that reads.

Every route sat behind a session cookie, which a home-automation module, a
Stream Deck plugin or a shell script cannot obtain — there is no browser to
hold it and no form to post. And what /api did expose was almost entirely
POST actions: plenty of ways to change things, almost none to ask.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import session, utcnow
from app.main import app
from tests.factories import iso, make_episode, make_series, pin, watch


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


@pytest.fixture
def keyed(db, admin_token):
    """A bare client plus a working key, with no cookie anywhere near it."""
    _, user_id = admin_token
    with session() as conn:
        key = auth.create_api_key(conn, user_id, "Home Assistant")
    with TestClient(app) as c:
        yield c, key


def seed(conn, user_id, *, title="Silo", days=1, has_file=0, runtime=45,
         episode=8, sonarr_episode_id=None):
    sid = make_series(conn, title, plex_rating_key="55", sonarr_id=7,
                      outlook="dated", pinned_by=user_id)
    eid = make_episode(
        conn, sid, season=3, episode=episode, title="Radio", runtime=runtime,
        air_date_utc=iso(days=days), has_file=has_file, in_plex=has_file,
        sonarr_episode_id=sonarr_episode_id,
        arrived_at=iso(days=days) if has_file else None,
    )
    return sid, eid


# ── Getting in ──


def test_a_key_works_without_a_cookie(keyed):
    c, key = keyed
    assert c.get("/api/summary", headers={"X-Api-Key": key}).status_code == 200


def test_bearer_works_too(keyed):
    """Most clients reach for Bearer; the *arr stack taught everyone
    X-Api-Key. Both, because this sits beside Sonarr."""
    c, key = keyed
    r = c.get("/api/summary", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200


def test_no_key_gets_a_401_not_a_login_page(keyed):
    """A machine needs an answer it can act on, not a redirect to a form it
    cannot fill in."""
    c, _ = keyed
    r = c.get("/api/summary")
    assert r.status_code == 401
    assert "API key" in r.json()["detail"]


def test_a_wrong_key_is_refused(keyed):
    c, _ = keyed
    r = c.get("/api/summary", headers={"X-Api-Key": "pnr_nonsense"})
    assert r.status_code == 401


def test_a_key_that_does_not_look_like_one_is_refused(keyed):
    c, _ = keyed
    assert c.get("/api/summary", headers={"X-Api-Key": "hunter2"}).status_code == 401


def test_the_key_is_stored_hashed(db, admin_token):
    """A key readable out of the database is a password written on the wall."""
    _, user_id = admin_token
    with session() as conn:
        key = auth.create_api_key(conn, user_id, "HA")
        rows = conn.execute("SELECT key_hash, prefix FROM api_keys").fetchall()
    assert key not in rows[0]["key_hash"]
    assert rows[0]["prefix"] == key[:8]


def test_use_is_recorded(keyed):
    c, key = keyed
    c.get("/api/summary", headers={"X-Api-Key": key})
    with session() as conn:
        assert conn.execute("SELECT last_used_at FROM api_keys").fetchone()[0]


def test_revoking_takes_effect(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        key = auth.create_api_key(conn, user_id, "HA")
        key_id = conn.execute("SELECT id FROM api_keys").fetchone()["id"]
    with TestClient(app) as c:
        assert c.get("/api/summary", headers={"X-Api-Key": key}).status_code == 200
        with session() as conn:
            assert auth.revoke_api_key(conn, user_id, key_id) is True
        assert c.get("/api/summary", headers={"X-Api-Key": key}).status_code == 401


def test_you_cannot_revoke_someone_elses_key(db, account):
    _, marc = account()
    _, bob = account("bob", "user")
    with session() as conn:
        auth.create_api_key(conn, marc, "Marc's")
        key_id = conn.execute("SELECT id FROM api_keys").fetchone()["id"]
        assert auth.revoke_api_key(conn, bob, key_id) is False


def test_a_key_carries_its_owners_role_not_more(db, account):
    """A standard user's key must not reach the admin-only routes."""
    _, bob = account("bob", "user")
    with session() as conn:
        key = auth.create_api_key(conn, bob, "Bob's")
    with TestClient(app) as c:
        assert c.get("/api/summary", headers={"X-Api-Key": key}).status_code == 200
        r = c.get("/api/backup", headers={"X-Api-Key": key})
    assert r.status_code == 403


def test_a_key_sees_only_its_owners_pins(db, account):
    _, marc = account()
    _, bob = account("bob", "user")
    with session() as conn:
        seed(conn, marc)
        bob_key = auth.create_api_key(conn, bob, "Bob's")
    with TestClient(app) as c:
        body = c.get("/api/schedule", headers={"X-Api-Key": bob_key}).json()
    assert body["episodes"] == []


# ── What it answers ──


def test_the_schedule_lists_upcoming_episodes(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, days=2)
    body = client.get("/api/schedule").json()
    assert body["count"] == 1
    ep = body["episodes"][0]
    assert ep["series"] == "Silo"
    assert ep["code"] == "S03E08"
    assert ep["runtime"] == 45
    assert ep["air_local"]
    assert ep["ends_local"]


def test_the_schedule_looks_back_a_little_by_default(client, admin_token):
    """"Did last night's episode arrive" is a breakfast question, and a feed
    starting at midnight tonight cannot answer it."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, days=-0.5, has_file=1)
    assert client.get("/api/schedule").json()["count"] == 1


def test_the_window_is_clamped(client, admin_token):
    body = client.get("/api/schedule?days=9999").json()
    assert body["to"]  # did not explode, and did not ask for ten years


def test_next_gives_one_episode(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, days=2)
        seed(conn, user_id, title="Severance", days=5, episode=1)
    assert client.get("/api/next").json()["episode"]["series"] == "Silo"


def test_next_is_null_when_nothing_is_coming(client):
    assert client.get("/api/next").json()["episode"] is None


def test_watching_reports_progress(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo", pinned_by=user_id)
        for number in (1, 2, 3):
            eid = make_episode(conn, sid, season=1, episode=number, has_file=1,
                               in_plex=1, runtime=45)
            if number == 1:
                watch(conn, user_id, eid)
    shows = client.get("/api/watching").json()["shows"]
    assert shows[0]["code"] == "S01E02"
    assert shows[0]["watched"] == 1
    assert shows[0]["owned"] == 3


def test_downloads_reports_stalling(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, sonarr_episode_id=555)
        conn.execute(
            "INSERT INTO download_queue (sonarr_episode_id, status, percent, "
            "first_seen_at, progress_at, updated_at) "
            "VALUES (555, 'downloading', 3.0, ?, ?, ?)",
            (iso(hours=-20), iso(hours=-19), utcnow()),
        )
    body = client.get("/api/downloads").json()
    assert body["stalled"] == 1
    assert body["items"][0]["stalled"] is True
    assert body["items"][0]["percent"] == 3.0


def test_arrivals_is_pollable(client, admin_token):
    """The trigger an automation actually wants: what landed, newest first,
    each stamped so a caller can act on anything newer than last time."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, days=-0.2, has_file=1)
    body = client.get("/api/arrivals").json()
    assert body["count"] == 1
    assert body["episodes"][0]["arrived_at"]


def test_arrivals_respects_the_window(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, days=-20, has_file=1)
    assert client.get("/api/arrivals?hours=2").json()["count"] == 0


def test_the_summary_answers_a_dashboard_in_one_call(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, days=2)
    body = client.get("/api/summary").json()
    assert body["pinned"] == 1
    assert body["next"]["series"] == "Silo"
    assert set(body["counts"]) >= {
        "airing_today", "upcoming", "overdue", "downloading", "stalled",
        "ready_to_watch", "unwatched_minutes", "gaps", "at_risk",
    }
    assert body["healthy"] is True
    assert body["generated_at"]


def test_the_summary_reports_a_failing_job(client, admin_token):
    """Not "is Pinnarr up" — the caller reached it. Whether what it says is
    still true, which is the part a dashboard should show."""
    from app.db import job_finished, job_started

    run = job_started("plex_library")
    job_finished(run, "error", "Plex unreachable")
    body = client.get("/api/summary").json()
    assert body["healthy"] is False
    assert "plex_library" in body["failing_jobs"]


def test_summary_json_is_serialisable_end_to_end(client, admin_token):
    """Row objects and datetimes have to be gone by the time they leave."""
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo", pinned_by=user_id)
        eid = make_episode(conn, sid, season=1, episode=1, has_file=1, in_plex=1,
                           runtime=45, air_date_utc=iso(days=-1),
                           arrived_at=iso(hours=-2))
        watch(conn, user_id, eid)
        pin(conn, user_id, sid)
    assert client.get("/api/summary").status_code == 200


# ── Managing keys through the page ──


def test_a_key_is_shown_once_and_then_never(client):
    r = client.post("/profile/keys", data={"name": "Stream Deck"}, follow_redirects=True)
    assert "Copy this now" in r.text
    assert "Stream Deck" in r.text
    assert "Copy this now" not in client.get("/profile").text


def test_the_list_shows_a_prefix_not_the_key(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        key = auth.create_api_key(conn, user_id, "HA")
    body = client.get("/profile").text
    assert key not in body
    assert key[:8] in body


def test_revoking_through_the_page(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        auth.create_api_key(conn, user_id, "HA")
        key_id = conn.execute("SELECT id FROM api_keys").fetchone()["id"]
    client.post(f"/profile/keys/{key_id}/revoke", follow_redirects=True)
    with session() as conn:
        assert auth.api_keys(conn, user_id) == []
