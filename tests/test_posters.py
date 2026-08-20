"""Poster sourcing.

Plex art only exists for shows already in the library, which left everything
Sonarr merely tracks — and almost all of Discover — with a blank card.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import auth, media
from app.clients.sonarr import _poster_from
from app.config import save_settings
from app.db import session, utcnow
from app.main import app

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
REMOTE = "https://artworks.thetvdb.com/banners/posters/silo.jpg"


def add(conn, *, plex_thumb=None, remote=None):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, poster_url, remote_poster, "
        "created_at, updated_at) VALUES ('Silo', 'silo', ?, ?, ?, ?)",
        (plex_thumb, remote, now, now),
    )
    return int(cur.lastrowid)


@pytest.fixture
def client(db, admin_token, tmp_path, monkeypatch):
    token, _ = admin_token
    monkeypatch.setattr(media, "cache_dir", lambda: tmp_path)
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


# ── Sonarr's payload ──


def test_the_poster_is_picked_out_of_sonarrs_images():
    payload = {
        "images": [
            {"coverType": "banner", "remoteUrl": "https://example.test/banner.jpg"},
            {"coverType": "poster", "remoteUrl": REMOTE},
        ]
    }
    assert _poster_from(payload) == REMOTE


def test_a_relative_mediacover_path_is_not_used():
    """Sonarr's own /MediaCover path needs an API key; remoteUrl does not."""
    payload = {"images": [{"coverType": "poster", "url": "/MediaCover/12/poster.jpg"}]}
    assert _poster_from(payload) is None


@pytest.mark.parametrize("payload", [{}, {"images": None}, {"images": ["junk"]}])
def test_missing_or_malformed_images_yield_nothing(payload):
    assert _poster_from(payload) is None


# ── Serving ──


@respx.mock
def test_a_series_only_sonarr_knows_about_still_gets_art(client):
    respx.get(REMOTE).mock(return_value=httpx.Response(200, content=PNG,
                                                       headers={"Content-Type": "image/jpeg"}))
    with session() as conn:
        sid = add(conn, remote=REMOTE)

    r = client.get(f"/poster/{sid}")
    assert r.status_code == 200
    assert r.content == PNG


@respx.mock
def test_the_second_request_is_served_from_cache(client):
    route = respx.get(REMOTE).mock(
        return_value=httpx.Response(200, content=PNG, headers={"Content-Type": "image/jpeg"})
    )
    with session() as conn:
        sid = add(conn, remote=REMOTE)

    client.get(f"/poster/{sid}")
    client.get(f"/poster/{sid}")
    assert route.call_count == 1


@respx.mock
def test_plex_art_wins_when_both_exist(client):
    """Plex art is the one you chose, and it is on the LAN."""
    save_settings({"plex_url": "http://plex.lan:32400", "plex_token": "tok"})
    plex = respx.get("http://plex.lan:32400/library/metadata/1/thumb").mock(
        return_value=httpx.Response(200, content=b"plex-art",
                                    headers={"Content-Type": "image/jpeg"})
    )
    remote = respx.get(REMOTE).mock(return_value=httpx.Response(200, content=PNG))
    with session() as conn:
        sid = add(conn, plex_thumb="/library/metadata/1/thumb", remote=REMOTE)

    assert client.get(f"/poster/{sid}").content == b"plex-art"
    assert plex.call_count == 1
    assert remote.call_count == 0


@respx.mock
def test_a_plex_failure_falls_through_to_sonarrs_art(client):
    """A Plex hiccup should not blank a whole page of cards."""
    save_settings({"plex_url": "http://plex.lan:32400", "plex_token": "tok"})
    respx.get("http://plex.lan:32400/library/metadata/1/thumb").mock(
        return_value=httpx.Response(500)
    )
    respx.get(REMOTE).mock(return_value=httpx.Response(200, content=PNG))
    with session() as conn:
        sid = add(conn, plex_thumb="/library/metadata/1/thumb", remote=REMOTE)

    assert client.get(f"/poster/{sid}").content == PNG


@respx.mock
def test_an_oversized_response_is_refused(client):
    respx.get(REMOTE).mock(
        return_value=httpx.Response(200, content=b"0" * (media.MAX_BYTES + 1))
    )
    with session() as conn:
        sid = add(conn, remote=REMOTE)

    r = client.get(f"/poster/{sid}")
    assert r.headers["content-type"].startswith("image/svg")


def test_no_source_at_all_still_serves_a_placeholder(client):
    with session() as conn:
        sid = add(conn)
    r = client.get(f"/poster/{sid}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg")
