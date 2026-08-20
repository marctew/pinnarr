"""Retiring finished pins, and nudging about shows that have picked up a date."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import session, utcnow
from app.jobs.notifications import notify_new_seasons
from app.main import app


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


@pytest.fixture
def pushes(monkeypatch):
    sent: list[dict] = []

    async def fake_send(title, message, *, tags="tv", priority="default",
                        click=None, topic=None):
        sent.append({"title": title, "message": message, "topic": topic})
        return True

    from app.jobs import notifications

    monkeypatch.setattr(notifications.ntfy, "send", fake_send)
    return sent


def add(conn, title, *, outlook="ended", next_airing=None, pinned_by=None, tvdb_id=None):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, tvdb_id, outlook, next_airing, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (title, title.lower(), tvdb_id, outlook, next_airing, now, now),
    )
    sid = int(cur.lastrowid)
    if pinned_by:
        conn.execute(
            "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
            (pinned_by, sid, now),
        )
        conn.execute("UPDATE series SET pinned = 1 WHERE id = ?", (sid,))
    return sid


# ── Retiring ──


def test_an_ended_pin_is_offered(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        add(conn, "Dark", outlook="ended", pinned_by=user_id)
    assert "Dark" in client.get("/retire").text


def test_a_cancelled_pin_is_offered(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        add(conn, "Firefly", outlook="cancelled", pinned_by=user_id)
    assert "Firefly" in client.get("/retire").text


def test_something_still_running_is_left_alone(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        add(conn, "Silo", outlook="dated", pinned_by=user_id)
    assert "Silo" not in client.get("/retire").text


def test_an_ended_show_with_a_finale_still_to_air_is_not_finished(client, admin_token):
    """TMDB calls a show Ended the moment the last season is announced as
    final — the finale can still be a fortnight away."""
    _, user_id = admin_token
    soon = (datetime.now(UTC) + timedelta(days=14)).isoformat()
    with session() as conn:
        add(conn, "Succession", outlook="ended", next_airing=soon, pinned_by=user_id)
    assert "Succession" not in client.get("/retire").text


def test_an_unpinned_ended_show_is_not_your_problem(client):
    with session() as conn:
        add(conn, "Dark", outlook="ended")
    assert "Dark" not in client.get("/retire").text


def test_retiring_unpins_them(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        add(conn, "Dark", outlook="ended", pinned_by=user_id, tvdb_id=1)
        add(conn, "Silo", outlook="dated", pinned_by=user_id, tvdb_id=2)

    body = client.post("/api/series/retire").json()
    assert body["retired"] == 1
    assert body["pinned_total"] == 1
    assert "Silo" in client.get("/library?pinned=pinned").text


def test_the_shared_flag_follows(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Dark", outlook="ended", pinned_by=user_id)
    client.post("/api/series/retire")
    with session() as conn:
        assert conn.execute(
            "SELECT pinned FROM series WHERE id = ?", (sid,)
        ).fetchone()["pinned"] == 0


def test_nothing_to_retire_says_so(client):
    assert "Nothing to retire" in client.get("/retire").text


def test_it_only_retires_your_own(db, account):
    admin_tok, _ = account()
    _, bob = account("bob", "user")
    with session() as conn:
        add(conn, "Dark", outlook="ended", pinned_by=bob)

    admin = TestClient(app)
    admin.cookies.set(auth.COOKIE, admin_tok)
    assert admin.post("/api/series/retire").json()["retired"] == 0
    with session() as conn:
        assert conn.execute("SELECT count(*) AS n FROM pins").fetchone()["n"] == 1


# ── New-season nudges ──


async def test_a_dated_show_you_do_not_follow_is_suggested(client, admin_token, pushes):
    _, user_id = admin_token
    soon = (datetime.now(UTC) + timedelta(days=20)).isoformat()
    with session() as conn:
        conn.execute("UPDATE users SET ntfy_topic = 'marc' WHERE id = ?", (user_id,))
        add(conn, "The Diplomat", outlook="dated", next_airing=soon)

    await notify_new_seasons()
    assert len(pushes) == 1
    assert "The Diplomat" in pushes[0]["message"]


async def test_something_you_already_pinned_is_not_suggested(client, admin_token, pushes):
    _, user_id = admin_token
    soon = (datetime.now(UTC) + timedelta(days=20)).isoformat()
    with session() as conn:
        conn.execute("UPDATE users SET ntfy_topic = 'marc' WHERE id = ?", (user_id,))
        add(conn, "Silo", outlook="dated", next_airing=soon, pinned_by=user_id)

    assert "nothing new" in await notify_new_seasons()
    assert pushes == []


async def test_the_same_show_is_not_suggested_twice(client, admin_token, pushes):
    """A weekly reminder you have already ignored is one you swipe away
    without reading."""
    _, user_id = admin_token
    soon = (datetime.now(UTC) + timedelta(days=20)).isoformat()
    with session() as conn:
        conn.execute("UPDATE users SET ntfy_topic = 'marc' WHERE id = ?", (user_id,))
        add(conn, "The Diplomat", outlook="dated", next_airing=soon)

    await notify_new_seasons()
    await notify_new_seasons()
    assert len(pushes) == 1


async def test_a_new_date_makes_it_worth_mentioning_again(client, admin_token, pushes):
    _, user_id = admin_token
    soon = (datetime.now(UTC) + timedelta(days=20)).isoformat()
    with session() as conn:
        conn.execute("UPDATE users SET ntfy_topic = 'marc' WHERE id = ?", (user_id,))
        sid = add(conn, "The Diplomat", outlook="dated", next_airing=soon)

    await notify_new_seasons()
    later = (datetime.now(UTC) + timedelta(days=40)).isoformat()
    with session() as conn:
        conn.execute("UPDATE series SET next_airing = ? WHERE id = ?", (later, sid))

    await notify_new_seasons()
    assert len(pushes) == 2


async def test_nobody_without_a_topic_is_nudged(client, pushes):
    soon = (datetime.now(UTC) + timedelta(days=20)).isoformat()
    with session() as conn:
        add(conn, "The Diplomat", outlook="dated", next_airing=soon)
    assert "skipped" in await notify_new_seasons()
    assert pushes == []


# ── Gaps ──


def gap_seed(conn, user_id, *, title="Line of Duty", missing=(4,), have=(1, 2, 3),
             season=2, tvdb_id=None, specials=False, synced=True):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, tvdb_id, pinned, episodes_synced_at, "
        "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
        (title, title.lower(), tvdb_id, now if synced else None, now, now),
    )
    sid = int(cur.lastrowid)
    aired = (datetime.now(UTC) - timedelta(days=400)).isoformat()

    def episode(number, has_file, in_season):
        conn.execute(
            "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
            "has_file, in_plex, monitored, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, 1, ?)",
            (sid, in_season, number, f"Episode {number}", aired, has_file, now),
        )

    for number in have:
        episode(number, 1, season)
    for number in missing:
        episode(number, 0, season)
    if specials:
        episode(99, 0, 0)

    conn.execute(
        "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
        (user_id, sid, now),
    )
    return sid


def test_a_hole_in_a_season_is_found(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        gap_seed(conn, user_id)
    body = client.get("/gaps").text
    assert "Line of Duty" in body
    assert "S02E04" in body


def test_a_complete_season_is_not_a_gap(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        gap_seed(conn, user_id, missing=(), have=(1, 2, 3))
    assert "Line of Duty" not in client.get("/gaps").text


def test_a_missing_special_is_not_a_hole_in_a_story(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        gap_seed(conn, user_id, missing=(), have=(1,), specials=True)
    assert "Line of Duty" not in client.get("/gaps").text


def test_something_not_yet_aired_is_not_missing(client, admin_token):
    _, user_id = admin_token
    now = utcnow()
    with session() as conn:
        cur = conn.execute(
            "INSERT INTO series (title, sort_title, pinned, created_at, updated_at) "
            "VALUES ('Silo', 'silo', 1, ?, ?)", (now, now),
        )
        sid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
            "has_file, in_plex, monitored, updated_at) "
            "VALUES (?, 3, 9, 'Later', ?, 0, 0, 1, ?)",
            (sid, (datetime.now(UTC) + timedelta(days=7)).isoformat(), now),
        )
        conn.execute(
            "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
            (user_id, sid, now),
        )
    assert "Silo" not in client.get("/gaps").text


def test_an_unpinned_show_is_not_your_problem(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = gap_seed(conn, user_id)
        conn.execute("DELETE FROM pins WHERE series_id = ?", (sid,))
    assert "Line of Duty" not in client.get("/gaps").text


def test_a_partial_sync_is_admitted_rather_than_implied(client, admin_token):
    """Without a full guide we only hold the calendar window, so "no gaps"
    would be a claim we cannot make."""
    _, user_id = admin_token
    with session() as conn:
        gap_seed(conn, user_id, synced=False)
    assert "synced window only" in client.get("/gaps").text


def test_with_nothing_pinned_it_says_so(client):
    assert "Pin something first" in client.get("/gaps").text
