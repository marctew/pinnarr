"""Episode state (SPEC §9) and the calendar view (§13)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import session
from app.episodes import AVAILABLE, AWAITING, MISSING, UPCOMING, episode_state
from app.main import app
from tests.factories import iso, make_episode, make_series, watch

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


def seed(conn, *, pinned=1, air_offset_days=2, has_file=0, outlook="dated", user_id=1):
    # Pins live in their own table now; series.pinned is only the derived
    # "anyone pinned this" flag. user 1 is the admin the fixture creates.
    sid = make_series(conn, "Severance", pinned=pinned, outlook=outlook,
                      pinned_by=user_id if pinned else None)
    make_episode(conn, sid, season=2, episode=7, title="Cold Harbor",
                 air_date_utc=iso(days=air_offset_days), has_file=has_file, in_plex=0)
    return sid


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
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


def test_an_empty_fortnight_still_says_what_is_coming(client):
    """The dots-with-no-information case: something is clearly scheduled, and
    the agenda window just misses it."""
    with session() as conn:
        seed(conn, air_offset_days=40)
    body = client.get("/").text
    assert "Nothing from your pinned shows in the next fortnight" in body
    assert "Next up" in body
    assert "Severance" in body


def test_the_month_view_lists_what_its_dots_are(client):
    with session() as conn:
        seed(conn, air_offset_days=40)
    ahead = (datetime.now(UTC) + timedelta(days=40)).strftime("%Y-%m")
    body = client.get(f"/?month={ahead}").text
    assert "S02E07" in body


def test_a_month_with_nothing_in_it_says_so(client):
    with session() as conn:
        seed(conn, air_offset_days=2)
    assert "Nothing from your pinned shows in January 2027" in client.get("/?month=2027-01").text


def test_day_cells_carry_the_show_names(client):
    with session() as conn:
        seed(conn, air_offset_days=3)
    assert "Severance S02E07" in client.get("/").text


def test_the_month_grid_names_the_shows_in_each_cell(client):
    with session() as conn:
        sid = seed(conn, air_offset_days=3)
    body = client.get("/").text
    assert f'href="/series/{sid}"' in body
    assert "cell-show" in body


def test_the_agenda_does_not_repeat_itself_in_the_month_list(client):
    """Viewing the current month, everything upcoming is already in the
    agenda above — the month list covers what has already aired."""
    with session() as conn:
        seed(conn, air_offset_days=3)
    body = client.get("/").text
    # The grid cell's tooltip carries the code too, so count rendered rows.
    assert body.count(">S02E07<") == 1
    assert "Nothing of yours has aired yet this month" in body


def test_a_past_episode_this_month_appears_under_earlier(client):
    with session() as conn:
        seed(conn, air_offset_days=-2, has_file=1)
    body = client.get("/").text
    assert "Earlier in" in body
    assert "S02E07" in body


def test_rows_carry_the_episode_title_and_poster(client):
    with session() as conn:
        sid = seed(conn, air_offset_days=3)
    body = client.get("/").text
    assert "Cold Harbor" in body
    assert f'src="/poster/{sid}"' in body


def test_days_are_labelled_relative_to_today(client):
    with session() as conn:
        seed(conn, air_offset_days=1)
    assert "tomorrow" in client.get("/").text


def test_the_month_grid_is_a_grid_not_a_table(client):
    """A table sizes columns to their content, so a long show name widened
    the column and the ellipsis never engaged."""
    with session() as conn:
        seed(conn, air_offset_days=3)
    body = client.get("/").text
    assert '<div class="month-grid">' in body
    assert "<table" not in body


def test_rows_show_the_air_time(client):
    with session() as conn:
        seed(conn, air_offset_days=3)
    assert '<span class="time">' in client.get("/").text


# ── Unmonitored episodes ──


def seed_unmonitored(conn, *, user_id=1, days=-5):
    """A special the user told Sonarr not to fetch — it aired, and it is
    never going to turn up."""
    sid = make_series(conn, "Taskmaster", outlook="dated", pinned_by=user_id)
    make_episode(conn, sid, season=0, episode=304, title="My Ultimate Episode",
                 air_date_utc=iso(days=days), has_file=0, in_plex=0, monitored=0)
    return sid


def test_an_unmonitored_episode_reads_as_not_wanted_not_missing():
    """Calling it missing cries wolf about the thing you decided against."""
    row = {**ep(NOW - timedelta(days=9)), "monitored": 0}
    assert episode_state(row, now=NOW) == "unmonitored"


def test_an_unmonitored_episode_you_do_have_still_reads_as_available():
    row = {**ep(NOW - timedelta(days=9), has_file=1), "monitored": 0}
    assert episode_state(row, now=NOW) == AVAILABLE


def test_unmonitored_episodes_are_hidden_from_the_calendar_by_default(client):
    with session() as conn:
        seed_unmonitored(conn)
    assert "My Ultimate Episode" not in client.get("/").text


def test_turning_the_setting_on_shows_them(client):
    from app.config import save_settings

    with session() as conn:
        seed_unmonitored(conn)
    save_settings({"show_unmonitored": "true", "show_specials": "true"})
    body = client.get("/").text
    assert "My Ultimate Episode" in body
    assert "not wanted" in body


def test_they_never_appear_under_aired_not_arrived(client):
    """Even shown, an episode nobody is chasing was never going to turn up."""
    from app.config import save_settings

    with session() as conn:
        seed_unmonitored(conn)
    save_settings({"show_unmonitored": "true", "show_specials": "true"})
    assert "Aired, not arrived" not in client.get("/").text


def test_the_json_feed_honours_the_setting(client):
    from app.config import save_settings

    with session() as conn:
        seed_unmonitored(conn, days=3)
    assert client.get("/api/calendar").json()["episodes"] == []

    save_settings({"show_unmonitored": "true", "show_specials": "true"})
    assert len(client.get("/api/calendar").json()["episodes"]) == 1


# ── Specials ──


def seed_special(conn, *, user_id=1, days=-5, monitored=1):
    """A Christmas one-off you never wanted. Sonarr does not reliably mark
    these unmonitored even when the season is, so the monitored toggle cannot
    hide them on its own."""
    sid = make_series(conn, "Taskmaster", outlook="dated", pinned_by=user_id)
    make_episode(conn, sid, season=0, episode=304, title="My Ultimate Episode",
                 air_date_utc=iso(days=days), has_file=0, in_plex=0, monitored=monitored)
    return sid


def test_a_monitored_special_is_still_hidden_by_default(client):
    """This is the case the monitored toggle could not reach: Sonarr says
    monitored, you say you never wanted it."""
    with session() as conn:
        seed_special(conn, monitored=1)
    assert "My Ultimate Episode" not in client.get("/").text


def test_a_special_never_reaches_aired_not_arrived(client):
    with session() as conn:
        seed_special(conn, monitored=1, days=-5)
    assert "Aired, not arrived" not in client.get("/").text


def test_turning_specials_on_shows_them(client):
    from app.config import save_settings

    with session() as conn:
        seed_special(conn, monitored=1)
    save_settings({"show_specials": "true"})
    assert "My Ultimate Episode" in client.get("/").text


def test_the_two_toggles_are_independent(client):
    """An unmonitored special needs both; a monitored one needs only the
    specials switch."""
    from app.config import save_settings

    with session() as conn:
        seed_special(conn, monitored=0)
    save_settings({"show_specials": "true"})
    assert "My Ultimate Episode" not in client.get("/").text

    save_settings({"show_unmonitored": "true"})
    assert "My Ultimate Episode" in client.get("/").text


def test_ordinary_seasons_are_untouched(client):
    with session() as conn:
        seed(conn, air_offset_days=3)
    assert "Cold Harbor" in client.get("/").text


# ── Watched episodes on the calendar ──


def seed_watched(conn, *, user_id=1, days=-6, watched=True):
    """Something that aired, is in Plex, and has been seen."""
    sid = make_series(conn, "The Undeclared War", sort_title="undeclared",
                      plex_rating_key="55", outlook="ended", pinned_by=user_id)
    eid = make_episode(conn, sid, season=2, episode=5, air_date_utc=iso(days=days),
                       has_file=1, in_plex=1, plex_rating_key="901")
    if watched:
        watch(conn, user_id, eid)
    return sid


def test_watched_episodes_are_hidden_by_default(client):
    """There is no point telling someone about an episode they have seen."""
    with session() as conn:
        seed_watched(conn)
    assert "The Undeclared War" not in client.get("/").text


def test_turning_the_setting_off_shows_them_marked(client):
    from app.config import save_settings

    with session() as conn:
        seed_watched(conn)
    save_settings({"hide_watched": "false"})
    body = client.get("/").text
    assert "The Undeclared War" in body
    assert "✓ watched" in body


def test_an_unwatched_episode_is_unaffected(client):
    with session() as conn:
        seed_watched(conn, watched=False)
    assert "The Undeclared War" in client.get("/").text


def test_the_pill_links_into_plex(client):
    from app.config import save_settings
    from app.db import set_setting

    with session() as conn:
        seed_watched(conn, watched=False)
    save_settings({"plex_url": "http://plex.lan:32400"})
    set_setting("plex_machine_id", "abc123")
    body = client.get("/").text
    assert "901" in body
    assert "abc123" in body


def test_the_json_feed_honours_the_setting_too(client):
    from app.config import save_settings

    with session() as conn:
        seed_watched(conn, days=3)
    assert client.get("/api/calendar").json()["episodes"] == []

    save_settings({"hide_watched": "false"})
    assert len(client.get("/api/calendar").json()["episodes"]) == 1
