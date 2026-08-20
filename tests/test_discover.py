"""Discover, and the on-demand episode guide.

Discover exists because a 2000-series library is mostly shows you have
forgotten about: every pin so far required you to remember one existed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import session
from app.main import app
from tests.factories import iso, make_episode, make_series


def add(conn, title, *, days=None, outlook="dated", sonarr_id=None, pinned_by=None):
    return make_series(
        conn, title, next_airing=iso(days=days) if days is not None else None,
        outlook=outlook, sonarr_id=sonarr_id, pinned_by=pinned_by,
    )


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


def test_something_airing_soon_shows_up(client):
    with session() as conn:
        add(conn, "Silo", days=3)
    body = client.get("/discover").text
    assert "Silo" in body
    assert "next 7 days" in body


def test_something_dated_further_out_is_its_own_section(client):
    with session() as conn:
        add(conn, "Andor", days=40)
    body = client.get("/discover").text
    assert "Andor" in body
    assert "further out" in body


def test_what_you_have_already_pinned_is_not_offered(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        add(conn, "Severance", days=3, pinned_by=user_id)
    assert "Severance" not in client.get("/discover").text


def test_another_users_pin_does_not_hide_it_from_you(client):
    """Discover is per user, like everything else about pins."""
    with session() as conn:
        bob = auth.create_user(conn, "bob", "password123", "user")
        add(conn, "Severance", days=3, pinned_by=bob)
    assert "Severance" in client.get("/discover").text


def test_something_already_aired_is_not_coming(client):
    with session() as conn:
        add(conn, "Old News", days=-3)
    assert "Old News" not in client.get("/discover").text


def test_announced_shows_are_offered_without_a_date(client):
    with session() as conn:
        add(conn, "Silo", days=None, outlook="announced")
    body = client.get("/discover").text
    assert "Silo" in body
    assert "no date yet" in body


def test_an_ended_show_with_no_date_is_not_offered(client):
    with session() as conn:
        add(conn, "Dark", days=None, outlook="ended")
    assert "Dark" not in client.get("/discover").text


def test_an_empty_discover_explains_itself(client):
    assert "Nothing unpinned has anything scheduled" in client.get("/discover").text


def test_pinning_from_discover_works(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add(conn, "Silo", days=3)
    assert client.post(f"/api/series/{sid}/pin").json()["pinned"] is True
    assert "Silo" not in client.get("/discover").text


# ── The episode guide ──


def test_the_page_says_the_window_is_partial_until_you_load_it(client):
    with session() as conn:
        sid = add(conn, "Silo", days=3, sonarr_id=12)
    body = client.get(f"/series/{sid}").text
    assert "Only the synced window is here" in body
    assert "Load full guide" in body


def test_a_series_sonarr_does_not_track_offers_no_button(client):
    with session() as conn:
        sid = add(conn, "Silo", days=3, sonarr_id=None)
    assert "Load full guide" not in client.get(f"/series/{sid}").text


def test_refreshing_without_sonarr_is_a_409_not_a_crash(client):
    with session() as conn:
        sid = add(conn, "Silo", days=3, sonarr_id=12)
    r = client.post(f"/api/series/{sid}/episodes")
    assert r.status_code == 409


def test_refreshing_an_unknown_series_is_a_404(client):
    assert client.post("/api/series/99999/episodes").status_code == 404


def test_episodes_are_grouped_into_seasons(client):
    with session() as conn:
        sid = add(conn, "Silo", days=3, sonarr_id=12)
        for season, number in ((1, 1), (1, 2), (2, 1)):
            make_episode(conn, sid, season=season, episode=number, title=f"E{number}",
                         air_date_utc="2026-01-01T00:00:00+00:00")
    body = client.get(f"/series/{sid}").text
    assert "Season 1" in body
    assert "Season 2" in body
    # Broadcast order, to match the episodes inside each season.
    assert body.index("Season 1") < body.index("Season 2")


def test_the_latest_season_is_the_one_left_open(client):
    """Ascending order puts season 1 first, which is the least interesting
    one — so the newest is expanded rather than the topmost."""
    with session() as conn:
        sid = add(conn, "Silo", days=3, sonarr_id=12)
        for season in (1, 2, 3):
            make_episode(conn, sid, season=season, title="x", air_date_utc="2026-01-01T00:00:00+00:00")
    body = client.get(f"/series/{sid}").text
    tag = '<details class="season" open>'
    assert body.count(tag) == 1
    assert "Season 3" in body.split(tag)[1].split("</summary>")[0]


def test_a_series_of_only_specials_still_opens_something(client):
    with session() as conn:
        sid = add(conn, "Oddity", days=3, sonarr_id=12)
        make_episode(conn, sid, season=0, title="x", air_date_utc="2026-01-01T00:00:00+00:00")
    assert "<details class=\"season\" open>" in client.get(f"/series/{sid}").text


def test_specials_sort_last_rather_than_first(client):
    """Season 0 is a footnote to a series, not the beginning of it."""
    with session() as conn:
        sid = add(conn, "Taskmaster", days=3, sonarr_id=12)
        for season in (0, 1):
            make_episode(conn, sid, season=season, title="x", air_date_utc="2026-01-01T00:00:00+00:00")
    body = client.get(f"/series/{sid}").text
    assert body.index("Season 1") < body.index("Specials")


# ── Collapsible sections ──


def test_sections_are_collapsible(client):
    with session() as conn:
        add(conn, "Silo", days=3)
    body = client.get("/discover").text
    assert '<details class="pane"' in body
    assert 'data-pane="week"' in body


def test_the_soonest_section_is_open_and_the_rest_are_not(client):
    """Four grids on one page; scrolling past the ones you are not using is
    not browsing."""
    with session() as conn:
        add(conn, "Soon", days=3)
        add(conn, "Later", days=40)
    body = client.get("/discover").text
    week = body.split('data-pane="week"')[1].split(">")[0]
    later = body.split('data-pane="later"')[1].split(">")[0]
    assert "open" in week
    assert "open" not in later


def test_each_section_shows_how_much_it_hides(client):
    with session() as conn:
        add(conn, "One", days=3)
        add(conn, "Two", days=3)
    body = client.get("/discover").text
    assert '<span class="count">2</span>' in body


def test_an_empty_section_is_not_rendered_at_all(client):
    with session() as conn:
        add(conn, "Silo", days=3)
    body = client.get("/discover").text
    assert 'data-pane="announced"' not in body
