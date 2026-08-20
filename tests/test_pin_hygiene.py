"""Pins that have gone cold, shows to carry on with, and pins at risk.

Three questions the data could already answer and no page was asking.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import session
from app.main import app
from app.repo import COLD_MONTHS, at_risk, cold_pins, continue_watching
from tests.factories import iso, make_episode, make_series, pin, watch


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


OLD = COLD_MONTHS * 30 + 30
RECENT = 5


def show(conn, user_id, title="Silo", *, owned=3, seen=0, pinned_days_ago=OLD,
         watched_days_ago=RECENT, **columns):
    """A pinned show with episodes in Plex, some of them watched."""
    sid = make_series(conn, title, **columns)
    pin(conn, user_id, sid, pinned_at=iso(days=-pinned_days_ago))
    for number in range(1, owned + 1):
        eid = make_episode(conn, sid, season=1, episode=number,
                           air_date_utc=iso(days=-100), has_file=1, in_plex=1,
                           runtime=45)
        if number <= seen:
            watch(conn, user_id, eid, watched_at=iso(days=-watched_days_ago))
    return sid


# ── Gone cold ──


def test_a_pin_you_have_never_started_goes_cold(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, user_id)
        assert [r["title"] for r in cold_pins(conn, user_id)] == ["Silo"]


def test_a_pin_you_stopped_watching_long_ago_goes_cold(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, user_id, seen=2, watched_days_ago=OLD)
        assert len(cold_pins(conn, user_id)) == 1


def test_a_pin_you_watched_recently_does_not(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, user_id, seen=2, watched_days_ago=RECENT)
        assert cold_pins(conn, user_id) == []


def test_a_pin_made_this_morning_is_not_neglected(db, admin_token):
    """Otherwise everything you pin is cold the moment you pin it."""
    _, user_id = admin_token
    with session() as conn:
        show(conn, user_id, pinned_days_ago=1)
        assert cold_pins(conn, user_id) == []


def test_a_pin_with_nothing_downloaded_is_patient_not_cold(db, admin_token):
    """Waiting on a season that has not started is not neglect."""
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo")
        pin(conn, user_id, sid, pinned_at=iso(days=-OLD))
        make_episode(conn, sid, season=2, episode=1, air_date_utc=iso(days=30))
        assert cold_pins(conn, user_id) == []


def test_a_show_the_retire_list_already_offers_is_not_listed_twice(db, admin_token):
    """One page, two sections. Offered twice and unpinned once is a bug."""
    _, user_id = admin_token
    with session() as conn:
        show(conn, user_id, outlook="ended")
        assert cold_pins(conn, user_id) == []


def test_cold_is_per_person(db, account):
    _, marc = account()
    _, bob = account("bob", "user")
    with session() as conn:
        sid = show(conn, marc, seen=3, watched_days_ago=RECENT)
        pin(conn, bob, sid, pinned_at=iso(days=-OLD))
        assert cold_pins(conn, marc) == []
        assert len(cold_pins(conn, bob)) == 1


def test_the_retire_page_offers_the_cold_ones(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, user_id)
    body = client.get("/retire").text
    assert "Gone cold" in body
    assert "Silo" in body


def test_retiring_the_cold_ones_unpins_them(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, user_id)
    assert client.post("/api/series/retire-cold").json()["retired"] == 1
    with session() as conn:
        assert cold_pins(conn, user_id) == []


def test_a_cold_retire_can_be_undone_like_any_other(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, user_id)
    client.post("/api/series/retire-cold")
    assert client.post("/api/series/retire-undo").json()["restored"] == 1


# ── Carry on watching ──


def test_a_part_watched_show_is_offered(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, user_id, owned=5, seen=2)
        rows = continue_watching(conn, user_id)
    assert len(rows) == 1
    assert rows[0]["episode"] == 3
    assert rows[0]["seen"] == 2
    assert rows[0]["owned"] == 5


def test_a_show_you_have_never_opened_is_not_a_continuation(db, admin_token):
    """That is a recommendation, and Ready already makes it."""
    _, user_id = admin_token
    with session() as conn:
        show(conn, user_id, owned=5, seen=0)
        assert continue_watching(conn, user_id) == []


def test_a_show_you_have_finished_is_not_offered(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, user_id, owned=3, seen=3)
        assert continue_watching(conn, user_id) == []


def test_the_most_recently_watched_comes_first(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, user_id, "Silo", owned=4, seen=1, watched_days_ago=20)
        show(conn, user_id, "Severance", owned=4, seen=1, watched_days_ago=2)
        rows = continue_watching(conn, user_id)
    assert [r["title"] for r in rows] == ["Severance", "Silo"]


def test_a_gap_does_not_stop_you_carrying_on(db, admin_token):
    """You are up to episode 3; episode 2 never arrived. The next thing you
    can actually watch is 3, not nothing."""
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo", pinned_by=user_id)
        first = make_episode(conn, sid, season=1, episode=1, has_file=1, in_plex=1)
        make_episode(conn, sid, season=1, episode=2, has_file=0, in_plex=0)
        make_episode(conn, sid, season=1, episode=3, has_file=1, in_plex=1)
        watch(conn, user_id, first)
        rows = continue_watching(conn, user_id)
    assert rows[0]["episode"] == 3


def test_the_list_is_capped(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        for n in range(6):
            show(conn, user_id, f"Show {n}", owned=3, seen=1)
        assert len(continue_watching(conn, user_id, limit=4)) == 4


def test_it_appears_on_the_calendar(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, user_id, owned=5, seen=2)
    body = client.get("/").text
    assert "Carry on watching" in body
    assert "S01E03" in body


def test_someone_elses_progress_is_not_yours(db, account):
    _, marc = account()
    _, bob = account("bob", "user")
    with session() as conn:
        sid = show(conn, marc, owned=4, seen=2)
        pin(conn, bob, sid)
        assert len(continue_watching(conn, marc)) == 1
        assert continue_watching(conn, bob) == []


# ── At risk ──


def test_a_show_with_holes_is_flagged(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo", sonarr_id=7, in_sonarr=1, pinned_by=user_id)
        make_episode(conn, sid, season=1, episode=1, has_file=1, in_plex=1,
                     air_date_utc=iso(days=-30))
        make_episode(conn, sid, season=1, episode=2, has_file=0, in_plex=0,
                     air_date_utc=iso(days=-20))
        flagged = at_risk(conn, user_id)
    assert len(flagged) == 1
    assert "never turned up" in flagged[0]["reasons"][0]


def test_holes_on_an_untracked_show_say_why(db, admin_token):
    """The distinction that matters: nothing is even trying to fetch them."""
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo", in_sonarr=0, pinned_by=user_id)
        make_episode(conn, sid, season=1, episode=1, has_file=0, in_plex=0,
                     air_date_utc=iso(days=-20))
        flagged = at_risk(conn, user_id)
    assert "nothing will fetch them" in flagged[0]["reasons"][0]


def test_a_finished_show_you_hold_in_full_is_not_at_risk(db, admin_token):
    """Not in Sonarr is only a problem if something still needs fetching."""
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo", outlook="ended", in_sonarr=0,
                          pinned_by=user_id)
        make_episode(conn, sid, season=1, episode=1, has_file=1, in_plex=1,
                     air_date_utc=iso(days=-400))
        assert at_risk(conn, user_id) == []


def test_a_returning_show_sonarr_does_not_know_about_is_flagged(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        make_series(conn, "Silo", in_sonarr=0, next_airing=iso(days=14),
                    pinned_by=user_id)
        flagged = at_risk(conn, user_id)
    assert "not tracking it" in flagged[0]["reasons"][0]


def test_a_plex_shortfall_is_flagged(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo", sonarr_id=7, in_sonarr=1,
                          plex_checked_at=iso(), pinned_by=user_id)
        make_episode(conn, sid, season=1, episode=1, has_file=1, in_plex=0,
                     air_date_utc=iso(days=-30))
        flagged = at_risk(conn, user_id)
    assert "Plex has not indexed" in flagged[0]["reasons"][-1]


def test_a_series_plex_never_checked_is_not_accused(db, admin_token):
    """in_plex = 0 means "never looked" until the availability job has run."""
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo", sonarr_id=7, in_sonarr=1,
                          plex_checked_at=None, pinned_by=user_id)
        make_episode(conn, sid, season=1, episode=1, has_file=1, in_plex=0,
                     air_date_utc=iso(days=-30))
        assert at_risk(conn, user_id) == []


def test_a_healthy_pin_is_not_flagged(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo", sonarr_id=7, in_sonarr=1,
                          plex_checked_at=iso(), pinned_by=user_id)
        make_episode(conn, sid, season=1, episode=1, has_file=1, in_plex=1,
                     air_date_utc=iso(days=-30))
        assert at_risk(conn, user_id) == []


def test_several_problems_are_reported_together(db, admin_token):
    """The point of joining them up: one show, both faults, one line."""
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo", sonarr_id=7, in_sonarr=1,
                          plex_checked_at=iso(), pinned_by=user_id)
        make_episode(conn, sid, season=1, episode=1, has_file=0, in_plex=0,
                     air_date_utc=iso(days=-30))
        make_episode(conn, sid, season=1, episode=2, has_file=1, in_plex=0,
                     air_date_utc=iso(days=-20))
        flagged = at_risk(conn, user_id)
    assert len(flagged[0]["reasons"]) == 2


def test_it_appears_on_the_gaps_page(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        make_series(conn, "Silo", in_sonarr=0, next_airing=iso(days=14),
                    pinned_by=user_id)
    body = client.get("/gaps").text
    assert "Needs attention" in body
    assert "not tracking it" in body
