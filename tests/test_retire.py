"""Retiring finished pins, and nudging about shows that have picked up a date."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import session, utcnow
from app.jobs.notifications import notify_new_seasons
from app.main import app
from tests.factories import iso, make_episode, make_series, pin


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


def add(conn, title, *, outlook="ended", next_airing=None, pinned_by=None, tvdb_id=None):
    return make_series(
        conn, title, tvdb_id=tvdb_id, outlook=outlook, next_airing=next_airing,
        pinned_by=pinned_by,
    )


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
    sid = make_series(conn, title, tvdb_id=tvdb_id,
                      episodes_synced_at=utcnow() if synced else None,
                      pinned_by=user_id)
    aired = iso(days=-400)

    def episode(number, has_file, in_season):
        make_episode(conn, sid, season=in_season, episode=number,
                     air_date_utc=aired, has_file=has_file, in_plex=0)

    for number in have:
        episode(number, 1, season)
    for number in missing:
        episode(number, 0, season)
    if specials:
        episode(99, 0, 0)
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
    with session() as conn:
        sid = make_series(conn, "Silo", pinned_by=user_id)
        make_episode(conn, sid, season=3, episode=9, title="Later",
                     air_date_utc=iso(days=7), has_file=0, in_plex=0)
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


# ── Undo ──


def pin_ids(conn, user_id):
    return {int(r["series_id"]) for r in conn.execute(
        "SELECT series_id FROM pins WHERE user_id = ?", (user_id,))}


def test_a_retire_can_be_undone(client, admin_token):
    """One click unpins every finished show at once. Before this it was
    irreversible while claiming in its own docstring not to be."""
    _, user_id = admin_token
    with session() as conn:
        a = add(conn, "Severance", pinned_by=user_id)
        b = add(conn, "Dark", pinned_by=user_id)

    assert client.post("/api/series/retire").json()["retired"] == 2
    with session() as conn:
        assert pin_ids(conn, user_id) == set()

    assert client.post("/api/series/retire-undo").json()["restored"] == 2
    with session() as conn:
        assert pin_ids(conn, user_id) == {a, b}


def test_undo_restores_the_original_pin_date(client, admin_token):
    """The library sorts by it. An undo that reorders your shelf is not one."""
    _, user_id = admin_token
    original = "2024-03-01T12:00:00+00:00"
    with session() as conn:
        sid = add(conn, "Severance", pinned_by=user_id)
        conn.execute(
            "UPDATE pins SET pinned_at = ?, notify = 0 WHERE series_id = ?",
            (original, sid),
        )

    client.post("/api/series/retire")
    client.post("/api/series/retire-undo")

    with session() as conn:
        row = conn.execute(
            "SELECT pinned_at, notify FROM pins WHERE series_id = ?", (sid,)
        ).fetchone()
    assert row["pinned_at"] == original
    assert row["notify"] == 0


def test_undo_restores_the_denormalised_flag(client, admin_token):
    """series.pinned drives every sync job. Leaving it at 0 would quietly
    stop Sonarr tags and the calendar window for a restored show."""
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Severance", pinned_by=user_id)
    client.post("/api/series/retire")
    client.post("/api/series/retire-undo")
    with session() as conn:
        assert conn.execute(
            "SELECT pinned FROM series WHERE id = ?", (sid,)
        ).fetchone()["pinned"] == 1


def test_undo_takes_the_most_recent_batch_only(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        first = add(conn, "Severance", pinned_by=user_id)
    client.post("/api/series/retire")
    with session() as conn:
        second = add(conn, "Dark", pinned_by=user_id)
    client.post("/api/series/retire")

    assert client.post("/api/series/retire-undo").json()["restored"] == 1
    with session() as conn:
        assert pin_ids(conn, user_id) == {second}
        assert first not in pin_ids(conn, user_id)


def test_undoing_twice_walks_back_through_the_batches(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        a = add(conn, "Severance", pinned_by=user_id)
    client.post("/api/series/retire")
    with session() as conn:
        b = add(conn, "Dark", pinned_by=user_id)
    client.post("/api/series/retire")

    client.post("/api/series/retire-undo")
    client.post("/api/series/retire-undo")
    with session() as conn:
        assert pin_ids(conn, user_id) == {a, b}


def test_undo_with_nothing_to_undo_is_not_an_error(client):
    r = client.post("/api/series/retire-undo")
    assert r.status_code == 200
    assert r.json()["restored"] == 0


def test_one_persons_undo_cannot_restore_anothers_pins(db, account):
    from app.repo import latest_retire_batch, retire, undo_retire

    _, marc = account()
    _, bob = account("bob", "user")
    with session() as conn:
        sid = add(conn, "Severance", pinned_by=marc)
        pin(conn, bob, sid)
        retire(conn, marc, [sid])
        assert latest_retire_batch(conn, bob) is None
        assert undo_retire(conn, bob, "whatever") == 0
        assert pin_ids(conn, bob) == {sid}


def test_the_undo_button_only_shows_when_there_is_something_to_undo(client, admin_token):
    _, user_id = admin_token
    assert "Undo the last retire" not in client.get("/retire").text
    with session() as conn:
        add(conn, "Severance", pinned_by=user_id)
    client.post("/api/series/retire")
    assert "Undo the last retire" in client.get("/retire").text


def test_housekeeping_expires_old_undos(db, admin_token):
    """Undo is a second thought, not an archive."""
    from app.jobs.housekeeping import RETIRE_UNDO_DAYS, prune_retired

    _, user_id = admin_token
    from app.repo import latest_retire_batch, retire

    with session() as conn:
        sid = add(conn, "Severance", pinned_by=user_id)
        retire(conn, user_id, [sid])
        stale = (datetime.now(UTC) - timedelta(days=RETIRE_UNDO_DAYS + 1)).isoformat()
        conn.execute("UPDATE retired_pins SET retired_at = ?", (stale,))

    assert prune_retired() == 1
    with session() as conn:
        assert latest_retire_batch(conn, user_id) is None
