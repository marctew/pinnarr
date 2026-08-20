"""Premiere and finale flags, and air-date change alerts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.clients.sonarr import SonarrEpisode
from app.db import session, utcnow
from app.episodes import milestone
from app.jobs.notifications import notify_schedule_changes
from app.repo import upsert_episode


def row(**kw):
    base = {"season": 1, "episode": 5, "finale_type": None}
    base.update(kw)
    return base


# ── Milestones ──


def test_the_first_episode_of_a_season_is_a_premiere():
    assert milestone(row(episode=1)) == "premiere"


def test_sonarrs_own_finale_label_is_trusted():
    """Guessing from the highest episode we hold would be wrong most of the
    time — the calendar window is two months, not a whole season."""
    assert milestone(row(episode=8, finale_type="season")) == "finale"
    assert milestone(row(episode=8, finale_type="series")) == "series finale"


def test_an_ordinary_episode_gets_nothing():
    assert milestone(row(episode=5)) == ""


def test_a_special_is_never_a_premiere_or_finale():
    """A Christmas one-off is not the start of anything."""
    assert milestone(row(season=0, episode=1)) == ""
    assert milestone(row(season=0, episode=1, finale_type="season")) == ""


def test_a_first_episode_that_is_also_a_finale_reads_as_the_finale():
    assert milestone(row(episode=1, finale_type="series")) == "series finale"


# ── Air-date moves ──


def episode(air: datetime | None, *, number=1):
    return SonarrEpisode(
        sonarr_episode_id=900 + number,
        sonarr_series_id=7,
        tvdb_id=None,
        season=3,
        episode=number,
        title=f"Episode {number}",
        air_date_utc=air.isoformat() if air else None,
        runtime=60,
        monitored=True,
        has_file=False,
    )


def add_series(conn, *, pinned_by=None, topic="marc"):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, pinned, created_at, updated_at) "
        "VALUES ('Silo', 'silo', 1, ?, ?)",
        (now, now),
    )
    sid = int(cur.lastrowid)
    if pinned_by:
        conn.execute(
            "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
            (pinned_by, sid, now),
        )
        conn.execute("UPDATE users SET ntfy_topic = ? WHERE id = ?", (topic, pinned_by))
    return sid


def changes(conn) -> list:
    return list(conn.execute("SELECT * FROM schedule_changes ORDER BY id"))


def test_a_date_moving_to_a_new_day_is_recorded(db):
    soon = datetime.now(UTC) + timedelta(days=7)
    with session() as conn:
        sid = add_series(conn)
        upsert_episode(conn, sid, episode(soon))
        upsert_episode(conn, sid, episode(soon + timedelta(days=7)))
        assert len(changes(conn)) == 1


def test_a_nudge_within_the_same_day_is_not_news(db):
    """Sonarr moves times by minutes routinely."""
    soon = datetime.now(UTC) + timedelta(days=7)
    with session() as conn:
        sid = add_series(conn)
        upsert_episode(conn, sid, episode(soon))
        upsert_episode(conn, sid, episode(soon + timedelta(minutes=20)))
        assert changes(conn) == []


def test_moving_something_that_already_aired_is_bookkeeping(db):
    past = datetime.now(UTC) - timedelta(days=30)
    with session() as conn:
        sid = add_series(conn)
        upsert_episode(conn, sid, episode(past))
        upsert_episode(conn, sid, episode(past - timedelta(days=2)))
        assert changes(conn) == []


def test_a_first_sighting_is_not_a_move(db):
    with session() as conn:
        sid = add_series(conn)
        upsert_episode(conn, sid, episode(datetime.now(UTC) + timedelta(days=7)))
        assert changes(conn) == []


def test_an_episode_gaining_a_date_from_nothing_is_not_flagged(db):
    """Undated to dated is the schedule appearing, not moving — the calendar
    shows that well enough on its own."""
    with session() as conn:
        sid = add_series(conn)
        upsert_episode(conn, sid, episode(None))
        upsert_episode(conn, sid, episode(datetime.now(UTC) + timedelta(days=7)))
        assert changes(conn) == []


# ── The announcement ──


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


async def test_a_move_is_announced_to_whoever_pinned_it(db, admin_token, pushes):
    _, user_id = admin_token
    soon = datetime.now(UTC) + timedelta(days=7)
    with session() as conn:
        sid = add_series(conn, pinned_by=user_id)
        upsert_episode(conn, sid, episode(soon))
        upsert_episode(conn, sid, episode(soon + timedelta(days=7)))

    await notify_schedule_changes()
    assert len(pushes) == 1
    assert "Silo S03E01 has moved" in pushes[0]["title"]
    assert "→" in pushes[0]["message"]


async def test_it_is_only_announced_once(db, admin_token, pushes):
    _, user_id = admin_token
    soon = datetime.now(UTC) + timedelta(days=7)
    with session() as conn:
        sid = add_series(conn, pinned_by=user_id)
        upsert_episode(conn, sid, episode(soon))
        upsert_episode(conn, sid, episode(soon + timedelta(days=7)))

    await notify_schedule_changes()
    await notify_schedule_changes()
    assert len(pushes) == 1


async def test_nobody_hears_about_a_show_they_have_not_pinned(db, admin_token, pushes):
    soon = datetime.now(UTC) + timedelta(days=7)
    with session() as conn:
        sid = add_series(conn)
        upsert_episode(conn, sid, episode(soon))
        upsert_episode(conn, sid, episode(soon + timedelta(days=7)))

    assert "no schedule changes" in await notify_schedule_changes()
    assert pushes == []


def test_a_premiere_is_badged_on_the_calendar(db, admin_token):
    from fastapi.testclient import TestClient

    from app import auth
    from app.main import app

    _, user_id = admin_token
    with session() as conn:
        sid = add_series(conn, pinned_by=user_id)
        upsert_episode(conn, sid, episode(datetime.now(UTC) + timedelta(days=3), number=1))

    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        body = c.get("/").text
    assert 'class="milestone"' in body
    assert "premiere" in body
