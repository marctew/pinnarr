"""Snoozing a pin, and waiting for a whole season.

Both are per-person decisions about one show, so they live on the pin rather
than the series: a household sharing a Sonarr does not thereby share an
opinion about whether to binge.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import session
from app.jobs.notifications import notify_pending, reconcile, weekly_digest
from app.main import app
from app.repo import is_snoozed, season_is_complete, snooze
from tests.factories import iso, make_episode, make_series, pin


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


def seed(conn, user_id, *, title="Silo", next_airing=None, topic="marc"):
    conn.execute("UPDATE users SET ntfy_topic = ? WHERE id = ?", (topic, user_id))
    return make_series(conn, title, next_airing=next_airing, pinned_by=user_id)


def arrived(conn, series_id, *, season=3, episode=1, minutes_ago=30, monitored=1,
            has_file=1, days_ago=1):
    return make_episode(
        conn, series_id, season=season, episode=episode, monitored=monitored,
        air_date_utc=iso(days=-days_ago), has_file=has_file, in_plex=has_file,
        arrived_at=iso(hours=-minutes_ago / 60),
    )


# ── Snooze, the state ──


def test_a_date_snooze_expires(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
        snooze(conn, user_id, sid, until=iso(days=30))
        assert is_snoozed(conn, user_id, sid) is True
        snooze(conn, user_id, sid, until=iso(days=-1))
        assert is_snoozed(conn, user_id, sid) is False


def test_until_it_returns_ends_when_a_date_appears(db, admin_token):
    """The useful snooze, and the one that cannot be written as a date."""
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
        snooze(conn, user_id, sid, until_dated=True)
        assert is_snoozed(conn, user_id, sid) is True

        conn.execute("UPDATE series SET next_airing = ? WHERE id = ?", (iso(days=20), sid))
        assert is_snoozed(conn, user_id, sid) is False


def test_choosing_a_date_cancels_until_it_returns(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
        snooze(conn, user_id, sid, until_dated=True)
        snooze(conn, user_id, sid, until=iso(days=-1))
        assert is_snoozed(conn, user_id, sid) is False


def test_waking_up_clears_both(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
        snooze(conn, user_id, sid, until=iso(days=30), until_dated=True)
        snooze(conn, user_id, sid)
        assert is_snoozed(conn, user_id, sid) is False


def test_a_snooze_belongs_to_one_person(db, account):
    _, marc = account()
    _, bob = account("bob", "user")
    with session() as conn:
        sid = seed(conn, marc)
        pin(conn, bob, sid)
        snooze(conn, marc, sid, until=iso(days=30))
        assert is_snoozed(conn, marc, sid) is True
        assert is_snoozed(conn, bob, sid) is False


# ── Snooze, the effect ──


async def test_a_snoozed_show_does_not_push_on_arrival(db, admin_token, pushes):
    from app.jobs.notifications import notify_arrival

    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
        arrived(conn, sid)
        snooze(conn, user_id, sid, until=iso(days=30))

    assert await notify_arrival(sid, 3, 1) == 0
    assert pushes == []


async def test_reconcile_does_not_catch_up_on_a_snoozed_show(db, admin_token, pushes):
    """Otherwise the nightly pass would deliver every push the snooze
    suppressed, which is the opposite of a snooze."""
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
        arrived(conn, sid, minutes_ago=120)
        snooze(conn, user_id, sid, until=iso(days=30))

    await reconcile()
    assert pushes == []


async def test_a_snoozed_show_is_left_out_of_the_digest(db, admin_token, pushes):
    from app.config import save_settings

    _, user_id = admin_token
    save_settings({"ntfy_url": "https://ntfy.sh", "ntfy_topic": "fallback"})
    with session() as conn:
        loud = seed(conn, user_id, title="Severance")
        quiet = seed(conn, user_id, title="Silo")
        make_episode(conn, loud, season=1, episode=1, air_date_utc=iso(days=2))
        make_episode(conn, quiet, season=1, episode=1, air_date_utc=iso(days=3))
        snooze(conn, user_id, quiet, until=iso(days=30))

    await weekly_digest()
    assert len(pushes) == 1
    assert "Severance" in pushes[0]["message"]
    assert "Silo" not in pushes[0]["message"]


async def test_waking_up_restores_notifications(db, admin_token, pushes):
    from app.jobs.notifications import notify_arrival

    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
        arrived(conn, sid)
        snooze(conn, user_id, sid, until=iso(days=30))
    assert await notify_arrival(sid, 3, 1) == 0

    with session() as conn:
        snooze(conn, user_id, sid)
    assert await notify_arrival(sid, 3, 1) == 1


# ── Season packs ──


def test_a_season_missing_an_aired_episode_is_incomplete(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
        arrived(conn, sid, episode=1)
        arrived(conn, sid, episode=2, has_file=0)
        assert season_is_complete(conn, sid, 3) is False


def test_an_unaired_episode_does_not_hold_a_season_open(db, admin_token):
    """A season still broadcasting would otherwise never be complete, and the
    pin would never notify at all."""
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
        arrived(conn, sid, episode=1)
        make_episode(conn, sid, season=3, episode=2, air_date_utc=iso(days=7))
        assert season_is_complete(conn, sid, 3) is True


def test_an_unmonitored_hole_does_not_hold_a_season_open(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
        arrived(conn, sid, episode=1)
        arrived(conn, sid, episode=2, has_file=0, monitored=0)
        assert season_is_complete(conn, sid, 3) is True


async def test_a_season_pack_pin_waits(db, admin_token, pushes):
    from app.config import save_settings

    _, user_id = admin_token
    save_settings({"notify_on_arrival": "true", "notify_batch_minutes": "5"})
    with session() as conn:
        sid = seed(conn, user_id)
        arrived(conn, sid, episode=1)
        arrived(conn, sid, episode=2, has_file=0)
        conn.execute("UPDATE pins SET season_pack = 1 WHERE series_id = ?", (sid,))

    detail = await notify_pending()
    assert pushes == []
    assert "waiting for a full season" in detail


async def test_a_season_pack_pin_pushes_once_complete(db, admin_token, pushes):
    from app.config import save_settings

    _, user_id = admin_token
    save_settings({"notify_on_arrival": "true", "notify_batch_minutes": "5"})
    with session() as conn:
        sid = seed(conn, user_id)
        arrived(conn, sid, episode=1)
        arrived(conn, sid, episode=2)
        conn.execute("UPDATE pins SET season_pack = 1 WHERE series_id = ?", (sid,))

    await notify_pending()
    assert len(pushes) == 1


async def test_patience_runs_out_so_one_bad_episode_cannot_silence_a_season(
    db, admin_token, pushes
):
    from app.config import save_settings
    from app.jobs.notifications import SEASON_PACK_PATIENCE_DAYS

    _, user_id = admin_token
    save_settings({"notify_on_arrival": "true", "notify_batch_minutes": "5"})
    with session() as conn:
        sid = seed(conn, user_id)
        arrived(conn, sid, episode=1,
                minutes_ago=(SEASON_PACK_PATIENCE_DAYS + 1) * 1440)
        arrived(conn, sid, episode=2, has_file=0)
        conn.execute("UPDATE pins SET season_pack = 1 WHERE series_id = ?", (sid,))

    await notify_pending()
    assert len(pushes) == 1


async def test_an_ordinary_pin_is_unaffected(db, admin_token, pushes):
    """The hold has to be opt-in, or every part-season becomes silent."""
    from app.config import save_settings

    _, user_id = admin_token
    save_settings({"notify_on_arrival": "true", "notify_batch_minutes": "5"})
    with session() as conn:
        sid = seed(conn, user_id)
        arrived(conn, sid, episode=1)
        arrived(conn, sid, episode=2, has_file=0)

    await notify_pending()
    assert len(pushes) == 1


# ── The routes ──


def test_snoozing_through_the_api(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
    body = client.post(f"/api/series/{sid}/snooze", data={"for": "3m"}).json()
    assert body["snoozed"] is True
    assert body["until"]


def test_snoozing_until_it_returns_through_the_api(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
    body = client.post(f"/api/series/{sid}/snooze", data={"for": "dated"}).json()
    assert body["until_dated"] is True
    assert body["until"] is None


def test_an_unknown_snooze_is_rejected_rather_than_ignored(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
    r = client.post(f"/api/series/{sid}/snooze", data={"for": "forever"})
    assert r.status_code == 400
    assert "unknown snooze" in r.json()["detail"]


def test_you_cannot_snooze_a_show_you_have_not_pinned(client, db):
    with session() as conn:
        sid = make_series(conn, "Unpinned")
    assert client.post(f"/api/series/{sid}/snooze", data={"for": "1m"}).status_code == 404


def test_the_season_pack_toggle_round_trips(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
    assert client.post(f"/api/series/{sid}/season-pack",
                       data={"wanted": "true"}).json()["season_pack"] is True
    assert "checked" in client.get(f"/series/{sid}").text


def test_the_series_page_shows_the_snooze_state(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
        snooze(conn, user_id, sid, until_dated=True)
    assert "snoozed until it returns" in client.get(f"/series/{sid}").text


def test_an_unpinned_show_offers_no_preferences(client, db):
    with session() as conn:
        sid = make_series(conn, "Unpinned")
    assert 'id="prefs"' not in client.get(f"/series/{sid}").text
