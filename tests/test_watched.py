"""Per-episode watch state.

Tautulli only ever gave us one timestamp per series, used for library
sorting, so marking something watched in Plex changed a sort order and
nothing else — Ready to Watch could never shrink.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import auth
from app.clients.tautulli import TautulliClient
from app.config import save_settings
from app.db import session, utcnow
from app.jobs.availability import sync_availability
from app.main import app
from app.repo import arrival_is_plausible, mark_watched

TAUTULLI = "http://tautulli.lan:8181"


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        save_settings({"tautulli_url": TAUTULLI, "tautulli_api_key": "key"})
        yield c


def seed(conn, user_id, *, plex_key="9001", episodes=(1, 2, 3), days_ago=2):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, plex_rating_key, pinned, "
        "created_at, updated_at) VALUES ('Silo', 'silo', ?, 1, ?, ?)",
        (plex_key, now, now),
    )
    sid = int(cur.lastrowid)
    aired = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    for number in episodes:
        conn.execute(
            "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
            "has_file, in_plex, monitored, runtime, updated_at) "
            "VALUES (?, 1, ?, ?, ?, 1, 1, 1, 45, ?)",
            (sid, number, f"Episode {number}", aired, now),
        )
    conn.execute(
        "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
        (user_id, sid, now),
    )
    return sid


# ── Marking ──


def test_marking_one_episode_leaves_the_others(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        assert mark_watched(conn, user_id, "9001", 1, 2, utcnow()) is True
        watched = conn.execute(
            "SELECT e.episode FROM episodes e "
            "JOIN episode_watches w ON w.episode_id = e.id"
        ).fetchall()
    assert [r["episode"] for r in watched] == [2]


def test_an_episode_we_do_not_hold_is_reported_not_invented(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        assert mark_watched(conn, user_id, "9001", 9, 9, utcnow()) is False


def test_the_earliest_play_is_kept(db, admin_token):
    """A rewatch is not when you first saw it."""
    _, user_id = admin_token
    first = "2026-01-01T20:00:00+00:00"
    second = "2026-08-01T20:00:00+00:00"
    with session() as conn:
        seed(conn, user_id)
        mark_watched(conn, user_id, "9001", 1, 1, second)
        mark_watched(conn, user_id, "9001", 1, 1, first)
        got = conn.execute(
            "SELECT w.watched_at FROM episode_watches w "
            "JOIN episodes e ON e.id = w.episode_id "
            "WHERE e.season = 1 AND e.episode = 1"
        ).fetchone()["watched_at"]
    assert got == first


# ── Ready shrinks ──


def test_a_watched_episode_drops_off_ready(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    assert client.get("/ready").text.count("Episode ") == 3

    with session() as conn:
        mark_watched(conn, user_id, "9001", 1, 2, utcnow())
    body = client.get("/ready").text
    assert "Episode 2" not in body
    assert "Episode 1" in body


def test_watching_everything_empties_the_page(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        for number in (1, 2, 3):
            mark_watched(conn, user_id, "9001", 1, number, utcnow())
    assert "Either you're caught up" in client.get("/ready").text


# ── The Tautulli feed ──


@respx.mock
async def test_only_completed_plays_count(client):
    """A show you gave up on twenty minutes in should stay on the list."""
    respx.get(f"{TAUTULLI}/api/v2").mock(
        return_value=httpx.Response(200, json={"response": {"result": "success", "data": {
            "data": [
                {"grandparent_rating_key": "9001", "parent_media_index": 1,
                 "media_index": 1, "watched_status": 1, "stopped": 1755000000},
                {"grandparent_rating_key": "9001", "parent_media_index": 1,
                 "media_index": 2, "watched_status": 0.5, "stopped": 1755000000},
            ]
        }}})
    )
    plays = await TautulliClient().watched_episodes()
    assert [p.episode for p in plays] == [1]


@respx.mock
async def test_malformed_history_rows_are_skipped(client):
    respx.get(f"{TAUTULLI}/api/v2").mock(
        return_value=httpx.Response(200, json={"response": {"result": "success", "data": {
            "data": [
                "junk",
                {"grandparent_rating_key": "", "watched_status": 1},
                {"grandparent_rating_key": "9001", "parent_media_index": None,
                 "media_index": 3, "watched_status": 1, "stopped": 1755000000},
                {"grandparent_rating_key": "9001", "parent_media_index": 1,
                 "media_index": 4, "watched_status": 1, "stopped": 1755000000},
            ]
        }}})
    )
    plays = await TautulliClient().watched_episodes()
    assert [p.episode for p in plays] == [4]


# ── Availability no longer invents arrivals ──


def test_a_back_catalogue_appearing_in_plex_is_not_an_arrival():
    """Seeing a 2020 episode for the first time is Pinnarr looking, not the
    episode landing."""
    assert arrival_is_plausible("2020-05-01T20:00:00+00:00") is False


def test_something_that_aired_yesterday_still_counts():
    yesterday = (datetime.now(UTC) - timedelta(hours=20)).isoformat()
    assert arrival_is_plausible(yesterday) is True


@respx.mock
async def test_the_availability_job_does_not_backdate_a_back_catalogue(db, admin_token,
                                                                      monkeypatch):
    _, user_id = admin_token
    save_settings({"plex_url": "http://plex.lan:32400", "plex_token": "t"})
    with session() as conn:
        seed(conn, user_id, days_ago=900)
        conn.execute("UPDATE episodes SET in_plex = 0, arrived_at = NULL")

    async def present(_self, _key):
        return {(1, 1), (1, 2), (1, 3)}

    from app.clients.plex import PlexClient

    monkeypatch.setattr(PlexClient, "episode_keys_present", present)
    await sync_availability()

    with session() as conn:
        stamped = conn.execute(
            "SELECT count(*) AS n FROM episodes WHERE arrived_at IS NOT NULL"
        ).fetchone()["n"]
        seen = conn.execute(
            "SELECT count(*) AS n FROM episodes WHERE in_plex = 1"
        ).fetchone()["n"]
    assert seen == 3
    assert stamped == 0


# ── Whose viewing is whose ──


def test_one_persons_viewing_is_not_anothers(db, account):
    """The whole reason this moved out of a column on episodes."""
    _, marc = account()
    _, bob = account("bob", "user")
    with session() as conn:
        seed(conn, marc)
        conn.execute(
            "INSERT INTO pins (user_id, series_id, pinned_at) "
            "SELECT ?, id, ? FROM series", (bob, utcnow()),
        )
        mark_watched(conn, marc, "9001", 1, 1, utcnow())

    from app.repo import watch_progress

    with session() as conn:
        assert watch_progress(conn, marc)[1]["seen"] == 1
        assert watch_progress(conn, bob)[1]["seen"] == 0


async def test_a_play_from_an_unclaimed_plex_account_is_dropped(db, admin_token, monkeypatch):
    """Crediting it to everybody would make watched mean less than nothing."""
    from app.clients.tautulli import EpisodePlay
    from app.jobs import tautulli_sync

    _, user_id = admin_token
    save_settings({"tautulli_url": TAUTULLI, "tautulli_api_key": "key"})
    with session() as conn:
        seed(conn, user_id)

    async def no_series(_self, length=2000):
        return {}

    async def one_play(_self, length=2000):
        return [EpisodePlay("9001", 1, 1, utcnow(), viewer="someone-else")]

    monkeypatch.setattr(TautulliClient, "last_watched_by_show", no_series)
    monkeypatch.setattr(TautulliClient, "watched_episodes", one_play)

    detail = await tautulli_sync.sync_tautulli_history()
    assert "nobody here has claimed" in detail
    with session() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM episode_watches"
        ).fetchone()["n"] == 0


async def test_a_play_from_a_claimed_account_is_credited(db, admin_token, monkeypatch):
    from app.clients.tautulli import EpisodePlay
    from app.jobs import tautulli_sync

    _, user_id = admin_token
    save_settings({"tautulli_url": TAUTULLI, "tautulli_api_key": "key"})
    with session() as conn:
        seed(conn, user_id)
        conn.execute("UPDATE users SET plex_username = 'MarcTew' WHERE id = ?", (user_id,))

    async def no_series(_self, length=2000):
        return {}

    async def one_play(_self, length=2000):
        # Case differs from what was stored; Plex is not consistent about it.
        return [EpisodePlay("9001", 1, 1, utcnow(), viewer="marctew")]

    monkeypatch.setattr(TautulliClient, "last_watched_by_show", no_series)
    monkeypatch.setattr(TautulliClient, "watched_episodes", one_play)

    await tautulli_sync.sync_tautulli_history()
    with session() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM episode_watches WHERE user_id = ?", (user_id,)
        ).fetchone()["n"] == 1


def test_the_library_shows_your_progress(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        mark_watched(conn, user_id, "9001", 1, 1, utcnow())
    assert "1/3 watched" in client.get("/library").text


def test_a_fully_watched_show_says_so(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        for number in (1, 2, 3):
            mark_watched(conn, user_id, "9001", 1, number, utcnow())
    assert "✓ watched" in client.get("/library").text


async def test_your_own_play_for_an_unsynced_episode_is_counted(db, admin_token, monkeypatch):
    """Silently dropping your own history is how "nothing shows as watched"
    happens with no way to find out why."""
    from app.clients.tautulli import EpisodePlay
    from app.jobs import tautulli_sync

    _, user_id = admin_token
    save_settings({"tautulli_url": TAUTULLI, "tautulli_api_key": "key"})
    with session() as conn:
        seed(conn, user_id, episodes=(1,))
        conn.execute("UPDATE users SET plex_username = 'mltew' WHERE id = ?", (user_id,))

    async def no_series(_self, length=2000):
        return {}

    async def plays(_self, length=2000):
        return [
            EpisodePlay("9001", 1, 1, utcnow(), viewer="mltew"),
            EpisodePlay("9001", 7, 9, utcnow(), viewer="mltew"),
        ]

    monkeypatch.setattr(TautulliClient, "last_watched_by_show", no_series)
    monkeypatch.setattr(TautulliClient, "watched_episodes", plays)

    detail = await tautulli_sync.sync_tautulli_history()
    assert "1 episode(s) watched" in detail
    assert "1 of yours are for episodes not synced here" in detail


# ── Per row on the series page ──


def test_a_watched_episode_says_so_on_its_own_row(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
        mark_watched(conn, user_id, "9001", 1, 2, utcnow())
    body = client.get(f"/series/{sid}").text
    assert "✓ watched" in body
    assert 'class="ep state-available seen"' in body


def test_an_unwatched_episode_keeps_its_availability(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
    body = client.get(f"/series/{sid}").text
    assert "✓ watched" not in body
    assert "in Plex" in body


def test_the_season_header_counts_what_you_have_seen(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
        mark_watched(conn, user_id, "9001", 1, 1, utcnow())
        mark_watched(conn, user_id, "9001", 1, 2, utcnow())
    assert "2/3 watched" in client.get(f"/series/{sid}").text


def test_a_season_you_have_not_started_still_reports_downloads(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = seed(conn, user_id)
    assert "3/3 downloaded" in client.get(f"/series/{sid}").text


def test_another_users_viewing_does_not_mark_your_rows(db, account):
    admin_tok, marc = account()
    _, bob = account("bob", "user")
    with session() as conn:
        sid = seed(conn, marc)
        mark_watched(conn, bob, "9001", 1, 1, utcnow())

    c = TestClient(app)
    c.cookies.set(auth.COOKIE, admin_tok)
    assert "✓ watched" not in c.get(f"/series/{sid}").text


# ── Partly watched ──


def part_watched(conn, user_id, *, seen_through=6, total=10, season=3):
    """A season you are partway through, which is the normal state of a show
    you are actually following."""
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, plex_rating_key, pinned, "
        "created_at, updated_at) VALUES ('Slow Horses', 'slow horses', '7777', 1, ?, ?)",
        (now, now),
    )
    sid = int(cur.lastrowid)
    for number in range(1, total + 1):
        conn.execute(
            "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
            "has_file, in_plex, monitored, updated_at) "
            "VALUES (?, ?, ?, ?, '2024-09-22T20:00:00+00:00', 1, 1, 1, ?)",
            (sid, season, number, f"Episode {number}", now),
        )
    conn.execute(
        "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
        (user_id, sid, now),
    )
    for number in range(1, seen_through + 1):
        mark_watched(conn, user_id, "7777", season, number, now)
    return sid


def test_the_series_page_says_where_you_are_up_to(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = part_watched(conn, user_id)
    body = client.get(f"/series/{sid}").text
    assert "Up next" in body
    assert "S03E07" in body


def test_the_part_watched_season_is_the_one_left_open(client, admin_token):
    """On a show you are midway through, the newest season is the least
    useful one to expand."""
    _, user_id = admin_token
    with session() as conn:
        sid = part_watched(conn, user_id, season=3)
        # A later season you have not started at all.
        conn.execute(
            "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
            "has_file, in_plex, monitored, updated_at) "
            "VALUES (?, 4, 1, 'Later', '2026-04-19T20:00:00+00:00', 1, 1, 1, ?)",
            (sid, utcnow()),
        )
    body = client.get(f"/series/{sid}").text
    tag = '<details class="season" open>'
    assert "Season 3" in body.split(tag)[1].split("</summary>")[0]


def test_a_season_you_have_started_counts_watched_not_downloads(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = part_watched(conn, user_id)
    assert "6/10 watched" in client.get(f"/series/{sid}").text


def test_finishing_everything_leaves_no_up_next(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = part_watched(conn, user_id, seen_through=10)
    assert "Up next" not in client.get(f"/series/{sid}").text


def test_the_library_card_names_the_next_episode(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        part_watched(conn, user_id)
    body = client.get("/library").text
    assert "6/10 watched · S03E07" in body


# ── Plex is authoritative ──


@respx.mock
async def test_plex_overrides_a_stale_play_record(db, admin_token, monkeypatch):
    """Tautulli logged a play; Plex says it is not watched. Plex wins, or
    nothing could ever be un-watched."""
    from app.clients.plex import PlexClient
    from app.jobs.watch_state import sync_watch_state

    _, user_id = admin_token
    save_settings({"plex_url": "http://plex.lan:32400"})
    with session() as conn:
        seed(conn, user_id, episodes=(1, 2))
        conn.execute("UPDATE users SET plex_token = 'tok' WHERE id = ?", (user_id,))
        mark_watched(conn, user_id, "9001", 1, 1, utcnow())
        mark_watched(conn, user_id, "9001", 1, 2, utcnow())

    async def state(_self, _key):
        return {(1, 1): True, (1, 2): False}

    monkeypatch.setattr(PlexClient, "view_state", state)
    detail = await sync_watch_state()

    assert "1 corrected" in detail
    with session() as conn:
        left = conn.execute(
            "SELECT e.episode FROM episode_watches w JOIN episodes e ON e.id = w.episode_id"
        ).fetchall()
    assert [r["episode"] for r in left] == [1]


@respx.mock
async def test_a_series_plex_will_not_answer_for_is_counted(db, admin_token, monkeypatch):
    """A silently truncated or failed response looks exactly like an
    unwatched season, which is how a whole season read as zero."""
    from app.clients.plex import PlexClient
    from app.jobs.watch_state import sync_watch_state

    _, user_id = admin_token
    save_settings({"plex_url": "http://plex.lan:32400"})
    with session() as conn:
        seed(conn, user_id)
        conn.execute("UPDATE users SET plex_token = 'tok' WHERE id = ?", (user_id,))

    async def boom(_self, _key):
        raise RuntimeError("nope")

    monkeypatch.setattr(PlexClient, "view_state", boom)
    assert "1 series Plex would not answer for" in await sync_watch_state()


@respx.mock
async def test_a_part_watched_episode_is_not_counted_as_seen(db, admin_token, monkeypatch):
    """Plex gives a part-watched episode a viewOffset and no viewCount. You
    have not seen it, and it should be what comes up next."""
    from app.clients.plex import PlexClient
    from app.jobs.watch_state import sync_watch_state

    _, user_id = admin_token
    save_settings({"plex_url": "http://plex.lan:32400"})
    with session() as conn:
        sid = seed(conn, user_id, episodes=(1, 2, 3))
        conn.execute("UPDATE users SET plex_token = 'tok' WHERE id = ?", (user_id,))

    async def state(_self, _key):
        return {(1, 1): True, (1, 2): False, (1, 3): False}

    monkeypatch.setattr(PlexClient, "view_state", state)
    await sync_watch_state()

    from app.repo import next_unwatched

    with session() as conn:
        nxt = next_unwatched(conn, user_id, sid)
    assert nxt["episode"] == 2
