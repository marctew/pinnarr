"""The Sonarr webhook (SPEC §14).

Sonarr's payload shape is undocumented, so the parser is written to be wrong
safely. These tests lean on that: malformed and unexpected payloads must be
recorded and acknowledged, never raised — Sonarr disables a connection that
keeps failing, and losing the feature permanently to report one bad delivery
is the worst possible trade.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, webhook
from app.config import save_settings
from app.db import session, utcnow
from app.main import app

SECRET = "a-real-webhook-secret"


def seed(conn, *, tvdb_id=371980, pinned_by=None, topic="marc"):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, tvdb_id, sonarr_id, outlook, "
        "created_at, updated_at) VALUES ('Severance', 'severance', ?, 55, 'dated', ?, ?)",
        (tvdb_id, now, now),
    )
    sid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
        "has_file, in_plex, monitored, updated_at) "
        "VALUES (?, 2, 7, 'Cold Harbor', '2026-08-19T20:00:00+00:00', 0, 0, 1, ?)",
        (sid, now),
    )
    if pinned_by:
        conn.execute(
            "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
            (pinned_by, sid, now),
        )
        conn.execute(
            "UPDATE series SET pinned = 1 WHERE id = ?", (sid,)
        )
        conn.execute(
            "UPDATE users SET ntfy_topic = ? WHERE id = ?", (topic, pinned_by)
        )
    return sid


def arrival(tvdb_id=371980, *, upgrade=False):
    return {
        "eventType": "Download",
        "isUpgrade": upgrade,
        "series": {"id": 55, "title": "Severance", "tvdbId": tvdb_id},
        "episodes": [{"seasonNumber": 2, "episodeNumber": 7, "title": "Cold Harbor"}],
    }


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        save_settings({"webhook_secret": SECRET})
        yield c


# ── The parser ──


def test_it_reads_what_it_needs_from_a_download_payload():
    d = webhook.parse(arrival())
    assert d.event_type == "download"
    assert d.tvdb_id == 371980
    assert d.episodes == [(2, 7)]


def test_a_multi_episode_file_yields_every_episode():
    payload = arrival()
    payload["episodes"].append({"seasonNumber": 2, "episodeNumber": 8})
    assert webhook.parse(payload).episodes == [(2, 7), (2, 8)]


def test_rubbish_in_the_episodes_list_is_skipped_not_fatal():
    payload = arrival()
    payload["episodes"] = [
        {"seasonNumber": 2, "episodeNumber": 7},
        {"seasonNumber": None, "episodeNumber": 3},
        {"seasonNumber": "x", "episodeNumber": "y"},
        "not even an object",
    ]
    assert webhook.parse(payload).episodes == [(2, 7)]


@pytest.mark.parametrize("payload", [None, [], "a string", 42, {}])
def test_the_parser_survives_anything(payload):
    assert webhook.parse(payload).event_type in {"unknown"}


# ── The endpoint ──


def test_the_wrong_secret_is_refused(client):
    r = client.post("/hooks/sonarr?secret=nope", json=arrival())
    assert r.status_code == 403


def test_no_secret_at_all_is_refused(client):
    r = client.post("/hooks/sonarr", json=arrival())
    assert r.status_code == 403


def test_the_receiver_is_disabled_until_a_secret_is_configured(db, admin_token):
    with TestClient(app) as c:
        save_settings({"webhook_secret": ""})
        assert c.post("/hooks/sonarr?secret=x", json=arrival()).status_code == 503


def test_sonarr_does_not_need_a_session(client):
    """Sonarr cannot log in, so the webhook sits outside the session gate."""
    anon = TestClient(app)
    assert anon.post(f"/hooks/sonarr?secret={SECRET}", json=arrival()).status_code == 200


def test_a_test_delivery_is_acknowledged_and_recorded(client):
    r = client.post(f"/hooks/sonarr?secret={SECRET}", json={"eventType": "Test"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert webhook.recent()[0]["event_type"] == "test"


def test_a_body_that_is_not_json_is_recorded_rather_than_raised(client):
    r = client.post(f"/hooks/sonarr?secret={SECRET}", content=b"<html>nope</html>")
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert webhook.recent()[0]["event_type"] == "unparseable"


def test_an_unknown_event_is_ignored_politely(client):
    r = client.post(f"/hooks/sonarr?secret={SECRET}", json={"eventType": "Rename"})
    assert r.status_code == 200
    assert "nothing to do" in r.json()["detail"]


def test_an_unknown_series_says_so_instead_of_failing(client):
    r = client.post(f"/hooks/sonarr?secret={SECRET}", json=arrival(tvdb_id=999999))
    assert r.status_code == 200
    assert "no local series" in r.json()["detail"]
    assert webhook.recent()[0]["handled"] == 0


def test_an_arrival_marks_the_episode_present(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, pinned_by=user_id)

    client.post(f"/hooks/sonarr?secret={SECRET}", json=arrival())

    with session() as conn:
        row = conn.execute(
            "SELECT has_file, arrived_at FROM episodes WHERE series_id = ?", (sid,)
        ).fetchone()
    assert row["has_file"] == 1
    assert row["arrived_at"]


def test_an_upgrade_does_not_move_arrived_at(client, admin_token):
    """On Upgrade fires for a 720p to 1080p replacement. That is not a new
    arrival, and it must not read as one."""
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, pinned_by=user_id)

    client.post(f"/hooks/sonarr?secret={SECRET}", json=arrival())
    with session() as conn:
        first = conn.execute(
            "SELECT arrived_at FROM episodes WHERE series_id = ?", (sid,)
        ).fetchone()["arrived_at"]

    client.post(f"/hooks/sonarr?secret={SECRET}", json=arrival(upgrade=True))
    with session() as conn:
        second = conn.execute(
            "SELECT arrived_at FROM episodes WHERE series_id = ?", (sid,)
        ).fetchone()["arrived_at"]
    assert first == second


def test_an_unpinned_series_is_marked_present_but_pushes_nothing(client):
    with session() as conn:
        sid = seed(conn)
    r = client.post(f"/hooks/sonarr?secret={SECRET}", json=arrival())
    assert "0 notification(s) sent" in r.json()["detail"]
    with session() as conn:
        assert conn.execute(
            "SELECT has_file FROM episodes WHERE series_id = ?", (sid,)
        ).fetchone()["has_file"] == 1


def test_the_delivery_log_is_capped(client):
    for _ in range(webhook.KEEP_DELIVERIES + 5):
        client.post(f"/hooks/sonarr?secret={SECRET}", json={"eventType": "Test"})
    with session() as conn:
        kept = conn.execute("SELECT count(*) AS n FROM webhook_log").fetchone()["n"]
    assert kept == webhook.KEEP_DELIVERIES


# ── The panel ──


def test_the_panel_offers_the_url_to_paste_into_sonarr(client):
    body = client.get("/settings/webhook").text
    assert SECRET in body
    assert "On Import" in body


def test_the_panel_says_so_when_no_secret_is_set(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        save_settings({"webhook_secret": ""})
        assert "receiver is disabled" in c.get("/settings/webhook").text


def test_a_standard_user_cannot_see_the_webhook_secret(db, account):
    account()
    token, _ = account("bob", "user")
    c = TestClient(app)
    c.cookies.set(auth.COOKIE, token)
    assert c.get("/settings/webhook").status_code == 403


# ── Per-series mute ──


def test_you_can_mute_a_series_you_pinned(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, pinned_by=user_id)

    assert client.post(f"/api/series/{sid}/notify", data={"notify": "false"}).json()["notify"] is False
    with session() as conn:
        assert conn.execute(
            "SELECT notify FROM pins WHERE user_id = ? AND series_id = ?", (user_id, sid)
        ).fetchone()["notify"] == 0


def test_muting_something_you_have_not_pinned_is_a_404(client):
    with session() as conn:
        sid = seed(conn)
    assert client.post(f"/api/series/{sid}/notify", data={"notify": "false"}).status_code == 404


# ── The push itself ──


@pytest.fixture
def pushes(monkeypatch):
    """Capture what would have gone to ntfy."""
    sent: list[dict] = []

    async def fake_send(title, message, *, tags="tv", priority="default",
                        click=None, topic=None):
        sent.append({"title": title, "message": message, "topic": topic})
        return True

    from app.jobs import notifications

    monkeypatch.setattr(notifications.ntfy, "send", fake_send)
    return sent


def test_an_arrival_pushes_to_whoever_pinned_it(client, admin_token, pushes):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, pinned_by=user_id, topic="marc-shows")

    client.post(f"/hooks/sonarr?secret={SECRET}", json=arrival())

    assert len(pushes) == 1
    assert pushes[0]["topic"] == "marc-shows"
    assert "Severance S02E07" in pushes[0]["title"]
    assert "Cold Harbor" in pushes[0]["message"]


def test_an_upgrade_does_not_buzz_you_twice(client, admin_token, pushes):
    """On Upgrade fires for every quality bump. Dedupe is the whole reason
    episode_notifications exists."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, pinned_by=user_id)

    client.post(f"/hooks/sonarr?secret={SECRET}", json=arrival())
    client.post(f"/hooks/sonarr?secret={SECRET}", json=arrival(upgrade=True))
    assert len(pushes) == 1


def test_everyone_who_pinned_it_gets_their_own_push(client, admin_token, account, pushes):
    _, admin_id = admin_token
    _, bob_id = account("bob", "user")
    with session() as conn:
        sid = seed(conn, pinned_by=admin_id, topic="marc-shows")
        conn.execute(
            "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
            (bob_id, sid, utcnow()),
        )
        conn.execute("UPDATE users SET ntfy_topic = 'bob-shows' WHERE id = ?", (bob_id,))

    client.post(f"/hooks/sonarr?secret={SECRET}", json=arrival())
    assert {p["topic"] for p in pushes} == {"marc-shows", "bob-shows"}


def test_a_muted_series_marks_present_without_pushing(client, admin_token, pushes):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, pinned_by=user_id)
    client.post(f"/api/series/{sid}/notify", data={"notify": "false"})

    client.post(f"/hooks/sonarr?secret={SECRET}", json=arrival())
    assert pushes == []


def test_someone_with_no_topic_is_skipped_quietly(client, admin_token, pushes):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, pinned_by=user_id, topic="")

    client.post(f"/hooks/sonarr?secret={SECRET}", json=arrival())
    assert pushes == []
