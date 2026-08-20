"""Ratings, taste-based suggestions, the Plex cross-check, and live states."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import auth
from app.config import save_settings
from app.db import session, utcnow
from app.jobs.suggest import refresh_suggestions
from app.main import app
from app.repo import plex_shortfall, suggested

TMDB = "https://api.themoviedb.org/3"


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        save_settings({"tmdb_api_key": "key"})
        yield c


def add(conn, title, *, tmdb_id=None, pinned_by=None, plex_key=None, sonarr_id=None,
        checked=True):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, tmdb_id, plex_rating_key, sonarr_id, "
        "plex_checked_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (title, title.lower(), tmdb_id, plex_key, sonarr_id,
         now if checked else None, now, now),
    )
    sid = int(cur.lastrowid)
    if pinned_by:
        conn.execute(
            "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
            (pinned_by, sid, now),
        )
        conn.execute("UPDATE series SET pinned = 1 WHERE id = ?", (sid,))
    return sid


def episode(conn, series_id, number, *, has_file=0, in_plex=0, season=1, rating=None,
             days_ago=2):
    # Recent by default: the calendar window is -30 to +120 days, so a date
    # from January would be correctly ignored and prove nothing.
    aired = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    conn.execute(
        "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
        "has_file, in_plex, monitored, rating, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (series_id, season, number, f"E{number}", aired, has_file, in_plex, rating, utcnow()),
    )


# ── Plex cross-check ──


def test_sonarr_holding_more_than_plex_is_flagged(client, admin_token):
    """A file Sonarr has and Plex hasn't indexed means a naming problem or a
    scan that never ran. Nothing else notices."""
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Silo", pinned_by=user_id, plex_key="1234")
        episode(conn, sid, 1, has_file=1, in_plex=1)
        episode(conn, sid, 2, has_file=1, in_plex=0)

    with session() as conn:
        rows = plex_shortfall(conn, user_id)
    assert len(rows) == 1
    assert rows[0]["have_sonarr"] == 2
    assert rows[0]["have_plex"] == 1


def test_a_series_in_step_is_not_flagged(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Silo", pinned_by=user_id, plex_key="1234")
        episode(conn, sid, 1, has_file=1, in_plex=1)
    with session() as conn:
        assert plex_shortfall(conn, user_id) == []


def test_specials_do_not_count_toward_the_shortfall(client, admin_token):
    """Season 0 is excluded everywhere else for the same reason."""
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Taskmaster", pinned_by=user_id, plex_key="1234")
        episode(conn, sid, 1, has_file=1, in_plex=1)
        episode(conn, sid, 99, has_file=1, in_plex=0, season=0)
    with session() as conn:
        assert plex_shortfall(conn, user_id) == []


def test_a_series_plex_never_matched_is_not_compared(client, admin_token):
    """Without a Plex key the availability job never checked it, so in_plex
    is zero for reasons that say nothing about Plex."""
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Silo", pinned_by=user_id, plex_key=None)
        episode(conn, sid, 1, has_file=1, in_plex=0)
    with session() as conn:
        assert plex_shortfall(conn, user_id) == []


def test_the_shortfall_appears_on_the_gaps_page(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Silo", pinned_by=user_id, plex_key="1234")
        episode(conn, sid, 1, has_file=1, in_plex=1)
        episode(conn, sid, 2, has_file=1, in_plex=0)
    body = client.get("/gaps").text
    assert "In Sonarr, not in Plex" in body
    assert "Silo" in body


# ── Suggestions ──


@respx.mock
async def test_recommendations_are_fetched_for_pins_only(client, admin_token):
    """A dozen calls for a dozen pins, not two thousand for the shelf."""
    _, user_id = admin_token
    with session() as conn:
        add(conn, "Severance", tmdb_id=95396, pinned_by=user_id)
        add(conn, "Unpinned", tmdb_id=111)

    route = respx.get(f"{TMDB}/tv/95396/recommendations").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 222, "name": "Devs"}]})
    )
    detail = await refresh_suggestions()
    assert "1/1" in detail
    assert route.call_count == 1


@respx.mock
async def test_only_shows_you_already_own_are_suggested(client, admin_token):
    """A recommendation you cannot watch is an advert."""
    _, user_id = admin_token
    with session() as conn:
        add(conn, "Severance", tmdb_id=95396, pinned_by=user_id)
        add(conn, "Devs", tmdb_id=222)

    respx.get(f"{TMDB}/tv/95396/recommendations").mock(
        return_value=httpx.Response(200, json={"results": [
            {"id": 222, "name": "Devs"},
            {"id": 999, "name": "Not In Your Library"},
        ]})
    )
    await refresh_suggestions()

    with session() as conn:
        rows = suggested(conn, user_id)
    assert [r["title"] for r in rows] == ["Devs"]


@respx.mock
async def test_something_already_pinned_is_not_suggested(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        add(conn, "Severance", tmdb_id=95396, pinned_by=user_id)
        add(conn, "Devs", tmdb_id=222, pinned_by=user_id)

    respx.get(f"{TMDB}/tv/95396/recommendations").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 222, "name": "Devs"}]})
    )
    await refresh_suggestions()
    with session() as conn:
        assert suggested(conn, user_id) == []


@respx.mock
async def test_a_show_several_pins_point_at_ranks_higher(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        add(conn, "Severance", tmdb_id=1, pinned_by=user_id)
        add(conn, "Silo", tmdb_id=2, pinned_by=user_id)
        add(conn, "Devs", tmdb_id=100)
        add(conn, "Fringe", tmdb_id=200)

    respx.get(f"{TMDB}/tv/1/recommendations").mock(
        return_value=httpx.Response(200, json={"results": [
            {"id": 100, "name": "Devs"}, {"id": 200, "name": "Fringe"}]})
    )
    respx.get(f"{TMDB}/tv/2/recommendations").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 100, "name": "Devs"}]})
    )
    await refresh_suggestions()

    with session() as conn:
        rows = suggested(conn, user_id)
    assert rows[0]["title"] == "Devs"


async def test_without_tmdb_it_does_nothing(db, admin_token):
    save_settings({"tmdb_api_key": ""})
    assert "TMDB not configured" in await refresh_suggestions()


@respx.mock
async def test_suggestions_show_on_discover(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        add(conn, "Severance", tmdb_id=95396, pinned_by=user_id)
        add(conn, "Devs", tmdb_id=222)
    respx.get(f"{TMDB}/tv/95396/recommendations").mock(
        return_value=httpx.Response(200, json={"results": [{"id": 222, "name": "Devs"}]})
    )
    await refresh_suggestions()

    body = client.get("/discover").text
    assert "Because of what you follow" in body
    assert "Devs" in body


# ── Ratings and live states ──


def test_a_rating_is_shown_on_the_guide(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Silo", pinned_by=user_id, sonarr_id=7)
        episode(conn, sid, 1, rating=8.6)
    body = client.get(f"/series/{sid}").text
    assert "8.6" in body
    assert 'class="heat"' in body


def test_a_season_with_no_ratings_draws_no_strip(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Silo", pinned_by=user_id, sonarr_id=7)
        episode(conn, sid, 1)
    assert 'class="heat"' not in client.get(f"/series/{sid}").text


def test_the_live_endpoint_reports_current_states(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Silo", pinned_by=user_id)
        episode(conn, sid, 1, has_file=1)
    body = client.get("/api/calendar/live").json()
    assert body["episodes"]
    assert next(iter(body["episodes"].values()))["state"] == "available"


def test_the_calendar_rows_carry_an_episode_id_to_update(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Silo", pinned_by=user_id)
        episode(conn, sid, 1, has_file=1)
    assert "data-episode=" in client.get("/").text


# ── The refresh says what it did about ratings ──


@respx.mock
def test_a_series_with_no_tmdb_id_says_why_there_are_no_ratings(client, admin_token):
    """Returning zero silently is how you end up wondering whether a feature
    exists at all."""
    save_settings({"sonarr_url": "http://sonarr.lan:8989", "sonarr_api_key": "k"})
    respx.get("http://sonarr.lan:8989/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[])
    )
    with session() as conn:
        sid = add(conn, "Silo", tmdb_id=None, sonarr_id=7)

    body = client.post(f"/api/series/{sid}/episodes").json()
    assert "no TMDB id" in body["ratings"]


@respx.mock
def test_without_tmdb_configured_it_says_so(client, admin_token):
    save_settings({"tmdb_api_key": "", "sonarr_url": "http://sonarr.lan:8989",
                   "sonarr_api_key": "k"})
    respx.get("http://sonarr.lan:8989/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[])
    )
    with session() as conn:
        sid = add(conn, "Silo", tmdb_id=95396, sonarr_id=7)

    body = client.post(f"/api/series/{sid}/episodes").json()
    assert "TMDB isn't configured" in body["ratings"]


@respx.mock
def test_a_successful_fetch_reports_the_count(client, admin_token):
    save_settings({"sonarr_url": "http://sonarr.lan:8989", "sonarr_api_key": "k"})
    respx.get("http://sonarr.lan:8989/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "title": "E1",
             "monitored": True, "hasFile": True},
        ])
    )
    respx.get(f"{TMDB}/tv/95396/season/1").mock(
        return_value=httpx.Response(200, json={"episodes": [
            {"episode_number": 1, "vote_average": 8.4},
        ]})
    )
    with session() as conn:
        sid = add(conn, "Silo", tmdb_id=95396, sonarr_id=7)

    body = client.post(f"/api/series/{sid}/episodes").json()
    assert body["rated"] == 1
    assert "1 episode(s) rated" in body["ratings"]


@respx.mock
def test_tmdb_having_no_scores_is_distinguished_from_a_failure(client, admin_token):
    save_settings({"sonarr_url": "http://sonarr.lan:8989", "sonarr_api_key": "k"})
    respx.get("http://sonarr.lan:8989/api/v3/episode").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "seasonNumber": 1, "episodeNumber": 1, "title": "E1",
             "monitored": True, "hasFile": False},
        ])
    )
    respx.get(f"{TMDB}/tv/95396/season/1").mock(
        return_value=httpx.Response(200, json={"episodes": []})
    )
    with session() as conn:
        sid = add(conn, "Silo", tmdb_id=95396, sonarr_id=7)

    assert "no scores" in client.post(f"/api/series/{sid}/episodes").json()["ratings"]


def test_the_progress_fill_is_not_pushed_off_the_left_edge(client, admin_token):
    """`have` named both the text beside the bar and the bar's own fill, so
    the text rule's margin-left: auto shoved the fill rightwards."""
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Silo", pinned_by=user_id, sonarr_id=7)
        episode(conn, sid, 1, has_file=1, in_plex=1)
    body = client.get(f"/series/{sid}").text
    assert 'class="seg seg-owned"' in body
    assert 'class="have" style=' not in body


def test_a_full_season_fills_the_bar(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Silo", pinned_by=user_id, sonarr_id=7)
        episode(conn, sid, 1, has_file=1, in_plex=1)
        episode(conn, sid, 2, has_file=1, in_plex=1)
    body = client.get(f"/series/{sid}").text
    assert "width: 100.0%" in body


def test_the_track_is_rendered_even_with_nothing_to_show(client, admin_token):
    """The column has to exist or the rows above and below it misalign."""
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Silo", pinned_by=user_id, sonarr_id=7)
        episode(conn, sid, 1)
    assert 'class="progress"' in client.get(f"/series/{sid}").text


def test_a_series_plex_has_never_been_asked_about_is_not_flagged(client, admin_token):
    """The bug: in_plex is 0 both when Plex lacks a series and when the
    availability job has not run yet, so a show pinned an hour ago was
    reported as entirely missing from a Plex that has all of it."""
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Ted Lasso", pinned_by=user_id, plex_key="1234", checked=False)
        for number in (1, 2, 3):
            episode(conn, sid, number, has_file=1, in_plex=0)
    with session() as conn:
        assert plex_shortfall(conn, user_id) == []


def test_once_checked_a_genuine_shortfall_is_flagged(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Ted Lasso", pinned_by=user_id, plex_key="1234", checked=True)
        for number in (1, 2, 3):
            episode(conn, sid, number, has_file=1, in_plex=0)
    with session() as conn:
        assert len(plex_shortfall(conn, user_id)) == 1


def test_the_season_bar_does_not_claim_a_file_is_in_plex(client, admin_token):
    """has_file and in_plex are different claims; the bar counts both, so it
    says downloaded rather than in Plex."""
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Ted Lasso", pinned_by=user_id, sonarr_id=7)
        episode(conn, sid, 1, has_file=1, in_plex=0)
    body = client.get(f"/series/{sid}").text
    assert "1/1 downloaded" in body
    assert "1/1 in Plex" not in body
