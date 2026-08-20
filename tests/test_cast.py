"""Where do I know them from — answered against your own shelf.

An IMDb page says this person has eighty credits. The useful answer is the
three of them you actually own, and which of those you have watched. Pinnarr
is the only thing in the stack that knows both halves.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import auth
from app.config import save_settings
from app.db import session
from app.jobs.cast_sync import sync_cast
from app.main import app
from app.repo import RECOGNISABLE_EPISODES, appearances, cast_for, familiar_faces
from tests.factories import make_episode, make_series, pin, watch

TMDB = "https://api.themoviedb.org/3"


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        save_settings({"tmdb_api_key": "key"})
        yield c


def add_person(conn, person_id, name, profile="/x.jpg"):
    conn.execute(
        "INSERT INTO people (tmdb_person_id, name, profile_path, updated_at) "
        "VALUES (?, ?, ?, datetime('now')) ON CONFLICT DO NOTHING",
        (person_id, name, profile),
    )


def add_role(conn, series_id, person_id, *, character="Someone", episodes=10, billing=0):
    conn.execute(
        "INSERT INTO series_cast (series_id, tmdb_person_id, character, "
        "episode_count, billing) VALUES (?, ?, ?, ?, ?)",
        (series_id, person_id, character, episodes, billing),
    )


def show(conn, title, *, user_id=None, watched=0, tmdb_id=None):
    sid = make_series(conn, title, tmdb_id=tmdb_id)
    if user_id:
        pin(conn, user_id, sid)
    for number in range(1, 4):
        eid = make_episode(conn, sid, season=1, episode=number, has_file=1, in_plex=1)
        if number <= watched:
            watch(conn, user_id, eid)
    return sid


# ── Pulling it in ──


@respx.mock
async def test_pinned_shows_are_fetched_first(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, "Silo", user_id=user_id, tmdb_id=111)

    route = respx.get(f"{TMDB}/tv/111/aggregate_credits").mock(
        return_value=httpx.Response(200, json={"cast": [
            {"id": 1, "name": "Rebecca Ferguson", "profile_path": "/r.jpg",
             "total_episode_count": 20, "order": 0,
             "roles": [{"character": "Juliette"}]},
        ]})
    )
    detail = await sync_cast()
    assert route.call_count == 1
    assert "1 pinned refreshed" in detail

    with session() as conn:
        row = conn.execute("SELECT * FROM people").fetchone()
    assert row["name"] == "Rebecca Ferguson"


@respx.mock
async def test_unpinned_shows_are_backfilled_too(client, admin_token):
    """The whole feature depends on it. "Where do I know them from" needs
    credits on both shows, so covering only pins means it can never fire for
    the thing you watched three years ago and never pinned — which is
    precisely the show that would answer the question."""
    _, user_id = admin_token
    with session() as conn:
        show(conn, "Silo", user_id=user_id, tmdb_id=111)
        show(conn, "Old Favourite", tmdb_id=222)

    for tmdb_id in (111, 222):
        respx.get(f"{TMDB}/tv/{tmdb_id}/aggregate_credits").mock(
            return_value=httpx.Response(200, json={"cast": [
                {"id": 1, "name": "Rebecca Ferguson", "total_episode_count": 20,
                 "order": 0, "roles": [{"character": "Someone"}]},
            ]})
        )
    detail = await sync_cast()
    assert "1 backfilled" in detail
    assert "whole library covered" in detail

    with session() as conn:
        covered = conn.execute(
            "SELECT count(DISTINCT series_id) AS n FROM series_cast"
        ).fetchone()["n"]
    assert covered == 2


@respx.mock
async def test_the_backfill_is_batched(client, admin_token):
    """Two thousand calls in one go would be rude to a free API."""
    _, user_id = admin_token
    with session() as conn:
        for n in range(5):
            show(conn, f"Show {n}", tmdb_id=500 + n)
    for n in range(5):
        respx.get(f"{TMDB}/tv/{500 + n}/aggregate_credits").mock(
            return_value=httpx.Response(200, json={"cast": []})
        )

    detail = await sync_cast(backfill=2)
    assert "2 backfilled" in detail
    assert "3 left to cover" in detail


@respx.mock
async def test_the_backfill_starts_with_what_you_have_watched(client, admin_token):
    """Those are the faces you would recognise, so they make the feature
    work soonest."""
    _, user_id = admin_token
    with session() as conn:
        show(conn, "Barely Touched", tmdb_id=601)
        show(conn, "Watched Loads", tmdb_id=602, user_id=user_id, watched=3)
        # Unpin it, so it competes as a backfill candidate rather than a pin.
        conn.execute("DELETE FROM pins")

    routes = {
        n: respx.get(f"{TMDB}/tv/{n}/aggregate_credits").mock(
            return_value=httpx.Response(200, json={"cast": []})
        )
        for n in (601, 602)
    }
    await sync_cast(backfill=1)
    assert routes[602].call_count == 1
    assert routes[601].call_count == 0


@respx.mock
async def test_a_covered_show_is_not_asked_about_again(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, "Old Favourite", tmdb_id=222)
    route = respx.get(f"{TMDB}/tv/222/aggregate_credits").mock(
        return_value=httpx.Response(200, json={"cast": []})
    )
    await sync_cast()
    await sync_cast()
    assert route.call_count == 1


@respx.mock
async def test_a_show_with_no_cast_listed_is_still_marked_covered(client):
    """Otherwise an empty answer is re-asked every night for ever."""
    with session() as conn:
        show(conn, "Obscure", tmdb_id=333)
    respx.get(f"{TMDB}/tv/333/aggregate_credits").mock(
        return_value=httpx.Response(200, json={"cast": []})
    )
    await sync_cast()
    with session() as conn:
        assert conn.execute(
            "SELECT cast_synced_at FROM series WHERE tmdb_id = 333"
        ).fetchone()["cast_synced_at"]


@respx.mock
async def test_a_recast_replaces_rather_than_accumulates(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, "Silo", user_id=user_id, tmdb_id=111)

    respx.get(f"{TMDB}/tv/111/aggregate_credits").mock(
        return_value=httpx.Response(200, json={"cast": [
            {"id": 1, "name": "First Actor", "total_episode_count": 5, "order": 0,
             "roles": [{"character": "The Sheriff"}]},
        ]})
    )
    await sync_cast()

    respx.get(f"{TMDB}/tv/111/aggregate_credits").mock(
        return_value=httpx.Response(200, json={"cast": [
            {"id": 2, "name": "Second Actor", "total_episode_count": 5, "order": 0,
             "roles": [{"character": "The Sheriff"}]},
        ]})
    )
    with session() as conn:
        # A month on, when the refresh is due again.
        conn.execute("UPDATE series SET cast_synced_at = '2020-01-01T00:00:00+00:00'")
    await sync_cast()

    with session() as conn:
        rows = conn.execute("SELECT tmdb_person_id FROM series_cast").fetchall()
    assert [r["tmdb_person_id"] for r in rows] == [2]


@respx.mock
async def test_a_show_whose_credits_fail_does_not_stop_the_run(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        show(conn, "Silo", user_id=user_id, tmdb_id=111)
        show(conn, "Severance", user_id=user_id, tmdb_id=222)

    respx.get(f"{TMDB}/tv/111/aggregate_credits").mock(
        return_value=httpx.Response(500, json={})
    )
    respx.get(f"{TMDB}/tv/222/aggregate_credits").mock(
        return_value=httpx.Response(200, json={"cast": [
            {"id": 9, "name": "Adam Scott", "total_episode_count": 20, "order": 0,
             "roles": [{"character": "Mark"}]},
        ]})
    )
    detail = await sync_cast()
    assert "1 failed" in detail
    assert "1 pinned refreshed" in detail


@respx.mock
async def test_roles_across_seasons_are_joined(client, admin_token):
    """aggregate_credits, not credits: a character who joined in season three
    is missing from the latter entirely."""
    _, user_id = admin_token
    with session() as conn:
        show(conn, "Silo", user_id=user_id, tmdb_id=111)
    respx.get(f"{TMDB}/tv/111/aggregate_credits").mock(
        return_value=httpx.Response(200, json={"cast": [
            {"id": 1, "name": "An Actor", "total_episode_count": 12, "order": 1,
             "roles": [{"character": "Young Him"}, {"character": "Old Him"}]},
        ]})
    )
    await sync_cast()
    with session() as conn:
        row = conn.execute("SELECT character FROM series_cast").fetchone()
    assert row["character"] == "Young Him, Old Him"


async def test_without_tmdb_it_does_nothing(db):
    save_settings({"tmdb_api_key": ""})
    assert "skipped" in await sync_cast()


# ── The cross-reference ──


def test_the_count_is_of_things_you_own_not_their_filmography(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        silo = show(conn, "Silo", user_id=user_id)
        mission = show(conn, "Mission", user_id=user_id)
        add_person(conn, 1, "Rebecca Ferguson")
        add_role(conn, silo, 1)
        add_role(conn, mission, 1)
        members = cast_for(conn, silo, user_id)
    assert members[0]["elsewhere"] == 1


def test_familiar_means_you_have_actually_watched_the_other_thing(db, admin_token):
    """The whole point. Being in something you own is trivia; being in
    something you watched is recognition."""
    _, user_id = admin_token
    with session() as conn:
        silo = show(conn, "Silo", user_id=user_id)
        other = show(conn, "The Other One", user_id=user_id, watched=3)
        unseen = show(conn, "Never Started", user_id=user_id)
        add_person(conn, 1, "Rebecca Ferguson")
        add_role(conn, silo, 1)
        add_role(conn, other, 1)
        add_person(conn, 2, "Nobody Familiar")
        add_role(conn, silo, 2)
        add_role(conn, unseen, 2)

        faces = familiar_faces(conn, silo, user_id)

    assert len(faces) == 1
    assert faces[0]["person"]["name"] == "Rebecca Ferguson"
    assert [r["title"] for r in faces[0]["seen_in"]] == ["The Other One"]


def test_a_one_scene_guest_is_not_a_face_you_would_place(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        silo = show(conn, "Silo", user_id=user_id)
        other = show(conn, "The Other One", user_id=user_id, watched=3)
        add_person(conn, 1, "Passing Extra")
        add_role(conn, silo, 1, episodes=RECOGNISABLE_EPISODES - 1)
        add_role(conn, other, 1, episodes=20)
        assert familiar_faces(conn, silo, user_id) == []


def test_the_show_you_are_looking_at_is_not_listed_as_elsewhere(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        silo = show(conn, "Silo", user_id=user_id, watched=3)
        add_person(conn, 1, "Rebecca Ferguson")
        add_role(conn, silo, 1)
        assert familiar_faces(conn, silo, user_id) == []


def test_one_persons_viewing_does_not_make_anothers_face_familiar(db, account):
    _, marc = account()
    _, bob = account("bob", "user")
    with session() as conn:
        silo = show(conn, "Silo", user_id=marc)
        other = show(conn, "The Other One", user_id=marc, watched=3)
        pin(conn, bob, silo)
        pin(conn, bob, other)
        add_person(conn, 1, "Rebecca Ferguson")
        add_role(conn, silo, 1)
        add_role(conn, other, 1)

        assert len(familiar_faces(conn, silo, marc)) == 1
        assert familiar_faces(conn, silo, bob) == []


# ── The pages ──


def test_the_series_page_says_who_you_know(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        silo = show(conn, "Silo", user_id=user_id)
        show(conn, "The Other One", user_id=user_id, watched=3)
        add_person(conn, 1, "Rebecca Ferguson")
        add_role(conn, silo, 1)
        other = conn.execute(
            "SELECT id FROM series WHERE title = 'The Other One'"
        ).fetchone()["id"]
        add_role(conn, other, 1)

    body = client.get(f"/series/{silo}").text
    assert "You've seen them before" in body
    assert "Rebecca Ferguson" in body
    assert "The Other One" in body


def test_the_series_page_omits_the_section_when_nobody_is_familiar(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        silo = show(conn, "Silo", user_id=user_id)
    assert "You've seen them before" not in client.get(f"/series/{silo}").text


def test_a_person_page_lists_only_what_you_own(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        silo = show(conn, "Silo", user_id=user_id, watched=2)
        add_person(conn, 1, "Rebecca Ferguson")
        add_role(conn, silo, 1)
    body = client.get("/person/1").text
    assert "Rebecca Ferguson" in body
    assert "Silo" in body
    assert "2 episodes watched" in body


def test_the_person_page_orders_by_what_you_have_watched(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        quiet = show(conn, "Barely Started", user_id=user_id, watched=0)
        loud = show(conn, "Watched Loads", user_id=user_id, watched=3)
        add_person(conn, 1, "Someone")
        add_role(conn, quiet, 1)
        add_role(conn, loud, 1)
        rows = appearances(conn, 1, user_id)
    assert [r["title"] for r in rows] == ["Watched Loads", "Barely Started"]


def test_an_unknown_person_is_a_404_not_an_empty_page(client):
    assert client.get("/person/99999").status_code == 404
