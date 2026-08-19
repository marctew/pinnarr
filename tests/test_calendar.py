"""Episode state (SPEC §9) and the calendar view (§13)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import session, utcnow
from app.episodes import AVAILABLE, AWAITING, MISSING, UPCOMING, episode_state
from app.main import app

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def ep(air: datetime | None, *, has_file=0, in_plex=0, season=1, episode=1):
    return {
        "air_date_utc": air.isoformat() if air else None,
        "has_file": has_file,
        "in_plex": in_plex,
        "season": season,
        "episode": episode,
    }


def test_a_future_episode_is_upcoming():
    assert episode_state(ep(NOW + timedelta(days=3)), now=NOW) == UPCOMING


def test_something_that_aired_yesterday_is_merely_awaited():
    assert episode_state(ep(NOW - timedelta(hours=20)), now=NOW) == AWAITING
    assert episode_state(ep(NOW - timedelta(hours=47)), now=NOW) == AWAITING


def test_airing_today_outranks_awaiting_for_something_aired_this_morning():
    """It aired at 11:00 and it is now noon. "Expected" is technically true
    and reads as though something has gone wrong; "airs today" is the useful
    thing to say."""
    assert episode_state(ep(NOW - timedelta(hours=1)), now=NOW) == "airing_today"


def test_past_the_grace_period_it_is_missing():
    assert episode_state(ep(NOW - timedelta(days=4)), now=NOW) == MISSING


def test_a_file_makes_it_available_whatever_the_date():
    assert episode_state(ep(NOW + timedelta(days=3), has_file=1), now=NOW) == AVAILABLE
    assert episode_state(ep(NOW - timedelta(days=9), has_file=1), now=NOW) == AVAILABLE


def test_in_plex_counts_even_without_a_sonarr_file():
    assert episode_state(ep(NOW - timedelta(days=9), in_plex=1), now=NOW) == AVAILABLE


def test_airing_today_is_judged_in_local_time_not_utc():
    """A US show at 01:00 UTC on the 20th is 02:00 BST — still 'today' only
    if you ask in London terms on the 20th, not the 19th."""
    late = datetime(2026, 8, 19, 23, 30, tzinfo=UTC)   # 00:30 BST on the 20th
    assert episode_state(ep(late), now=NOW, tz="Europe/London") == UPCOMING

    now_on_20th = datetime(2026, 8, 20, 6, 0, tzinfo=UTC)
    assert episode_state(ep(late), now=now_on_20th, tz="Europe/London") == "airing_today"


def test_an_undated_episode_does_not_crash_and_reads_as_upcoming():
    assert episode_state(ep(None), now=NOW) == UPCOMING


# ── The view ──


def seed(conn, *, pinned=1, air_offset_days=2, has_file=0, outlook="dated"):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, pinned, outlook, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Severance", "severance", pinned, outlook, now, now),
    )
    sid = int(cur.lastrowid)
    air = datetime.now(UTC) + timedelta(days=air_offset_days)
    conn.execute(
        "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
        "has_file, in_plex, monitored, updated_at) VALUES (?, 2, 7, 'Cold Harbor', ?, ?, 0, 1, ?)",
        (sid, air.isoformat(), has_file, now),
    )
    return sid


@pytest.fixture
def client(db):
    with TestClient(app) as c:
        yield c


def test_with_nothing_pinned_the_calendar_says_so(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Nothing pinned yet" in r.text


def test_a_pinned_episode_shows_in_the_agenda(client):
    with session() as conn:
        seed(conn)
    body = client.get("/").text
    assert "Severance" in body
    assert "S02E07" in body


def test_an_unpinned_series_is_not_on_the_calendar(client):
    with session() as conn:
        seed(conn, pinned=0)
    assert "Severance" not in client.get("/").text


def test_a_long_overdue_episode_gets_its_own_section(client):
    with session() as conn:
        seed(conn, air_offset_days=-5)
    body = client.get("/").text
    assert "Aired, not arrived" in body


def test_an_episode_already_in_plex_is_not_overdue(client):
    with session() as conn:
        seed(conn, air_offset_days=-5, has_file=1)
    assert "Aired, not arrived" not in client.get("/").text


def test_dormant_pins_are_collapsed_rather_than_celebrated(client):
    with session() as conn:
        seed(conn, outlook="dormant", air_offset_days=-400)
    body = client.get("/").text
    assert "Dormant (1)" in body
    assert "candidates to unpin" in body


def test_the_month_can_be_paged(client):
    with session() as conn:
        seed(conn)
    assert "September 2026" in client.get("/?month=2026-09").text


def test_a_nonsense_month_falls_back_to_now(client):
    assert client.get("/?month=banana").status_code == 200


def test_the_json_feed_carries_derived_state(client):
    with session() as conn:
        seed(conn)
    body = client.get("/api/calendar").json()
    assert body["episodes"][0]["series"] == "Severance"
    assert body["episodes"][0]["title"] == "Cold Harbor"
    assert body["episodes"][0]["season"] == 2
    assert body["episodes"][0]["state"] == "upcoming"


def test_the_json_feed_honours_an_explicit_window(client):
    with session() as conn:
        seed(conn, air_offset_days=40)
    assert client.get("/api/calendar").json()["episodes"] == []
    wide = client.get("/api/calendar?start=2026-01-01&end=2027-01-01").json()
    assert len(wide["episodes"]) == 1
