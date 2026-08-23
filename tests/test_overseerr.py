"""Linking out to Overseerr, and the right-click that gets you there.

Link-only on purpose. Pinnarr never calls Overseerr for anything a page
needs, so there is nothing for an API key to authenticate — and an unused
credential sitting in the settings table is a thing to leak, not a feature.
The one call it does make is the connection test, which needs no key.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import auth
from app.config import get_settings, save_settings
from app.db import session
from app.links import overseerr_person, overseerr_search
from app.main import app
from tests.factories import make_series, pin

OVERSEERR = "http://overseerr.lan:5055"


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


def cast(conn, series_id, person_id=1, name="Rebecca Ferguson", episodes=10):
    conn.execute(
        "INSERT INTO people (tmdb_person_id, name, profile_path, updated_at) "
        "VALUES (?, ?, '/r.jpg', datetime('now')) ON CONFLICT DO NOTHING",
        (person_id, name),
    )
    conn.execute(
        "INSERT INTO series_cast (series_id, tmdb_person_id, character, "
        "episode_count, billing) VALUES (?, ?, 'Someone', ?, 0)",
        (series_id, person_id, episodes),
    )


# ── The links ──


def test_a_person_link_uses_the_tmdb_id(db):
    """Overseerr routes people by TMDB id, which is the same id Pinnarr
    already stores — so this is exact, not a search that hopes."""
    save_settings({"overseerr_url": OVERSEERR})
    assert overseerr_person(12345) == f"{OVERSEERR}/person/12345"


def test_a_search_link_escapes_the_name(db):
    save_settings({"overseerr_url": OVERSEERR})
    assert overseerr_search("Bill Nighy") == f"{OVERSEERR}/search?query=Bill%20Nighy"


def test_without_a_url_there_is_no_link(db):
    """A missing link is invisible; a link that 404s is a bug report."""
    save_settings({"overseerr_url": ""})
    assert overseerr_person(12345) is None
    assert overseerr_search("Bill Nighy") is None


def test_a_trailing_slash_does_not_double_up(db):
    save_settings({"overseerr_url": f"{OVERSEERR}/"})
    assert overseerr_person(1) == f"{OVERSEERR}/person/1"


def test_no_person_no_link(db):
    save_settings({"overseerr_url": OVERSEERR})
    assert overseerr_person(None) is None


# ── The setting ──


def test_the_url_is_on_the_settings_page(client):
    body = client.get("/settings").text
    assert 'name="overseerr_url"' in body
    assert "no API key to give it" in body


def test_the_url_round_trips_through_the_form(client):
    client.post("/settings", data={"overseerr_url": OVERSEERR}, follow_redirects=False)
    assert get_settings().overseerr_url == OVERSEERR


def test_configured_is_just_the_url(db):
    save_settings({"overseerr_url": OVERSEERR})
    assert get_settings().overseerr_configured is True
    save_settings({"overseerr_url": ""})
    assert get_settings().overseerr_configured is False


# ── The connection test ──


@respx.mock
async def test_the_tester_reports_the_version(client):
    save_settings({"overseerr_url": OVERSEERR})
    respx.get(f"{OVERSEERR}/api/v1/status").mock(
        return_value=httpx.Response(200, json={"version": "1.33.2"})
    )
    body = client.post("/api/settings/test/overseerr").json()
    assert body["ok"] is True
    assert "1.33.2" in body["message"]


@respx.mock
async def test_something_that_is_not_an_overseerr_is_called_out(client):
    """A reverse proxy that answers 200 with a login page is the usual
    version of this going wrong."""
    save_settings({"overseerr_url": OVERSEERR})
    respx.get(f"{OVERSEERR}/api/v1/status").mock(
        return_value=httpx.Response(200, json={"nope": True})
    )
    body = client.post("/api/settings/test/overseerr").json()
    assert body["ok"] is False
    assert "not like an Overseerr" in body["message"]


async def test_the_tester_says_when_nothing_is_saved(client):
    save_settings({"overseerr_url": ""})
    body = client.post("/api/settings/test/overseerr").json()
    assert body["ok"] is False
    assert "No URL saved" in body["message"]


# ── The right-click ──


def test_a_cast_name_carries_what_the_menu_needs(client, admin_token):
    _, user_id = admin_token
    save_settings({"overseerr_url": OVERSEERR})
    with session() as conn:
        sid = make_series(conn, "Silo")
        pin(conn, user_id, sid)
        cast(conn, sid)

    body = client.get(f"/series/{sid}").text
    assert 'data-person="1"' in body
    assert 'data-person-name="Rebecca Ferguson"' in body
    assert f'data-overseerr="{OVERSEERR}/person/1"' in body
    assert "data-overseerr-search=" in body


def test_without_overseerr_the_name_still_offers_the_rest(client, admin_token):
    """The menu is not only about Overseerr — TMDB and the person page work
    whether or not it is configured."""
    _, user_id = admin_token
    save_settings({"overseerr_url": ""})
    with session() as conn:
        sid = make_series(conn, "Silo")
        pin(conn, user_id, sid)
        cast(conn, sid)

    body = client.get(f"/series/{sid}").text
    assert 'data-person="1"' in body
    assert "data-overseerr=" not in body


def test_the_menu_is_on_every_page(client):
    """Wired once in the base template, so anywhere cast turns up next
    behaves the same without being told to."""
    body = client.get("/library").text
    assert 'id="ctx"' in body
    assert "contextmenu" in body


def test_the_person_page_is_right_clickable_too(client, admin_token):
    _, user_id = admin_token
    save_settings({"overseerr_url": OVERSEERR})
    with session() as conn:
        sid = make_series(conn, "Silo")
        pin(conn, user_id, sid)
        cast(conn, sid)
    body = client.get("/person/1").text
    assert 'data-person="1"' in body
    assert "Look up on Overseerr" in body
