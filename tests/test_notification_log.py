"""What Pinnarr pushed, and whether ntfy took it.

A notification that does not arrive had three indistinguishable
explanations: the job never fired, ntfy refused it, or the phone ate it. The
first two are knowable and were simply not written down.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, notify
from app.db import session
from app.main import app
from tests.factories import make_episode, make_series


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


@pytest.fixture
def refusing(monkeypatch):
    """ntfy that takes the request and says no."""
    async def fake_send(*_args, **_kwargs):
        return False

    monkeypatch.setattr(notify.ntfy, "send", fake_send)


def rows():
    with session() as conn:
        return notify.history(conn)


# ── Recording ──


async def test_a_successful_push_is_recorded(db, admin_token, pushes):
    _, user_id = admin_token
    await notify.send("Silo S03E08 is in Plex", "Radio", kind="arrival",
                      user_id=user_id, topic="marc")
    row = rows()[0]
    assert row["title"] == "Silo S03E08 is in Plex"
    assert row["body"] == "Radio"
    assert row["kind"] == "arrival"
    assert row["topic"] == "marc"
    assert row["ok"] == 1


async def test_a_refused_push_is_recorded_too(db, admin_token, refusing):
    """The whole point: a push that failed has to be visible, or the log
    only ever confirms what already worked."""
    _, user_id = admin_token
    assert await notify.send("Nope", "body", kind="arrival", user_id=user_id) is False
    row = rows()[0]
    assert row["ok"] == 0
    assert row["detail"]


async def test_the_body_is_stored_as_sent(db, admin_token, pushes):
    """A log saying "a digest went out" cannot answer "did it list the right
    episodes"."""
    _, user_id = admin_token
    body = "Mon 01 Sep\n  Silo S03E08\n  Severance S02E07"
    await notify.send("This week: 2 episode(s)", body, kind="digest", user_id=user_id)
    assert rows()[0]["body"] == body


async def test_logging_failure_does_not_swallow_the_push(db, pushes, monkeypatch):
    """Bookkeeping is not allowed to lose a notification."""
    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(notify, "session", boom)
    assert await notify.send("Still sent", "body", kind="arrival") is True


# ── Through the real jobs ──


async def test_an_arrival_notification_shows_up_in_the_history(db, admin_token, pushes):
    from app.jobs.notifications import notify_arrival

    _, user_id = admin_token
    with session() as conn:
        conn.execute("UPDATE users SET ntfy_topic = 'marc' WHERE id = ?", (user_id,))
        sid = make_series(conn, "Silo", pinned_by=user_id)
        make_episode(conn, sid, season=3, episode=8, title="Radio",
                     has_file=1, in_plex=1)

    assert await notify_arrival(sid, 3, 8) == 1
    row = rows()[0]
    assert row["kind"] == "arrival"
    assert row["user_id"] == user_id


async def test_the_settings_test_push_is_logged(client, pushes):
    """Otherwise the history looks broken the first time you check it."""
    from app.config import save_settings

    save_settings({"ntfy_url": "https://ntfy.sh", "ntfy_topic": "marc"})
    assert client.post("/api/settings/test/ntfy").json()["ok"] is True
    assert rows()[0]["kind"] == "test"


# ── The page ──


async def test_the_page_lists_your_pushes(client, admin_token, pushes):
    _, user_id = admin_token
    await notify.send("Silo S03E08 is in Plex", "Radio", kind="arrival",
                      user_id=user_id, topic="marc")
    body = client.get("/notifications").text
    assert "Silo S03E08 is in Plex" in body
    assert "Episode arrived" in body
    assert "✓ sent" in body


async def test_a_failure_is_marked_on_the_page(client, admin_token, refusing):
    _, user_id = admin_token
    await notify.send("Nope", "body", kind="arrival", user_id=user_id)
    assert "✕ failed" in client.get("/notifications").text


def test_an_empty_history_says_so(client):
    assert "Nothing sent yet" in client.get("/notifications").text


async def test_you_do_not_see_someone_elses_notifications(db, account, pushes):
    marc_token, marc = account()
    _, bob = account("bob", "user")
    await notify.send("Bobs show", "body", kind="arrival", user_id=bob, topic="bob")
    await notify.send("Marcs show", "body", kind="arrival", user_id=marc, topic="marc")

    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, marc_token)
        body = c.get("/notifications").text
    assert "Marcs show" in body
    assert "Bobs show" not in body


async def test_an_admin_can_see_everyone(db, account, pushes):
    token, _ = account()
    _, bob = account("bob", "user")
    await notify.send("Bobs show", "body", kind="arrival", user_id=bob, topic="bob")

    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        assert "Bobs show" in c.get("/notifications?scope=all").text


async def test_a_standard_user_cannot_widen_the_scope(db, account, pushes):
    """The switch is admin-only, and so is the query behind it."""
    _, marc = account()
    bob_token, _ = account("bob", "user")
    await notify.send("Marcs show", "body", kind="arrival", user_id=marc, topic="marc")

    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, bob_token)
        assert "Marcs show" not in c.get("/notifications?scope=all").text
