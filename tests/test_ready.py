"""Ready to watch.

The calendar is built around anticipation. This is the other half: for
anyone who watches after a download rather than on transmission, "what can I
put on now" is the question they actually have.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import session, utcnow
from app.main import app
from app.repo import READY_DAYS


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


def seed(conn, user_id, *, title="Silo", episodes=(1,), days_ago=1, has_file=1,
         pinned=True, tvdb_id=None):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, tvdb_id, pinned, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (title, title.lower(), tvdb_id, int(pinned), now, now),
    )
    sid = int(cur.lastrowid)
    arrived = (
        (datetime.now(UTC) - timedelta(days=days_ago)).isoformat() if has_file else None
    )
    for number in episodes:
        conn.execute(
            "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
            "has_file, in_plex, monitored, arrived_at, updated_at) "
            "VALUES (?, 3, ?, ?, '2026-08-01T20:00:00+00:00', ?, 0, 1, ?, ?)",
            (sid, number, f"Episode {number}", has_file, arrived, now),
        )
    if pinned:
        conn.execute(
            "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
            (user_id, sid, now),
        )
    return sid


def test_a_recent_arrival_is_offered(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    body = client.get("/ready").text
    assert "Silo" in body
    assert "Episode 1" in body


def test_nothing_arrived_says_so(client):
    assert "Either you're caught up" in client.get("/ready").text


def test_something_that_has_not_arrived_is_not_offered(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, has_file=0)
    assert "Silo" not in client.get("/ready").text


def test_an_old_arrival_drops_off(client, admin_token):
    """It stays a shortlist rather than becoming an inventory."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, days_ago=READY_DAYS + 2)
    assert "Silo" not in client.get("/ready").text


def test_an_unpinned_series_is_not_offered(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, pinned=False)
    assert "Silo" not in client.get("/ready").text


def test_another_users_pin_does_not_appear(db, account):
    admin_tok, _ = account()
    bob_tok, bob = account("bob", "user")
    with session() as conn:
        seed(conn, bob, title="Silo")

    admin = TestClient(app)
    admin.cookies.set(auth.COOKIE, admin_tok)
    assert "Silo" not in admin.get("/ready").text

    bob_client = TestClient(app)
    bob_client.cookies.set(auth.COOKIE, bob_tok)
    assert "Silo" in bob_client.get("/ready").text


def test_episodes_are_grouped_into_one_decision_per_series(client, admin_token):
    """Four episodes of one show is one viewing decision, not four."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, episodes=(1, 2, 3, 4))
    body = client.get("/ready").text
    assert body.count('class="ready"') == 1
    assert "4 episodes" in body


def test_series_are_ordered_by_what_landed_most_recently(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, title="Older", days_ago=5, tvdb_id=1)
        seed(conn, user_id, title="Newer", days_ago=1, tvdb_id=2)
    body = client.get("/ready").text
    assert body.index("Newer") < body.index("Older")


def test_arrival_is_described_in_plain_language(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, days_ago=1)
    assert "yesterday" in client.get("/ready").text
