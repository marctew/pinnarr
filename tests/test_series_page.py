"""The per-series page, reachable from both the calendar and the library."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import session, utcnow
from app.main import app


def seed(conn, **kw):
    now = utcnow()
    fields = {
        "sort_title": "severance", "year": 2022, "network": "Apple TV+",
        "overview": "Mark leads a team of office workers.", "outlook": "dated",
        "sonarr_status": "continuing", "tmdb_status": "Returning Series",
        "in_plex": 1, "in_sonarr": 1, "pinned": 0, "tvdb_id": 371980,
        "latest_season": 2, "latest_aired_season": 2,
    }
    fields.update(kw)
    cols = ", ".join(["title", "created_at", "updated_at", *fields])
    marks = ", ".join(["?"] * (3 + len(fields)))
    cur = conn.execute(
        f"INSERT INTO series ({cols}) VALUES ({marks})",
        ["Severance", now, now, *fields.values()],
    )
    sid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
        "has_file, in_plex, monitored, updated_at) VALUES (?, 2, 7, 'Cold Harbor', ?, 1, 1, 1, ?)",
        (sid, (datetime.now(UTC) + timedelta(days=3)).isoformat(), now),
    )
    if fields.get("pinned"):
        conn.execute(
            "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (1, ?, ?)", (sid, now)
        )
    conn.execute("INSERT INTO genres (name) VALUES ('Sci-Fi')")
    gid = conn.execute("SELECT id FROM genres WHERE name = 'Sci-Fi'").fetchone()["id"]
    conn.execute("INSERT INTO series_genres (series_id, genre_id) VALUES (?, ?)", (sid, gid))
    return sid


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


def test_the_page_shows_what_the_grid_cannot(client):
    with session() as conn:
        sid = seed(conn)
    body = client.get(f"/series/{sid}").text
    assert "Mark leads a team" in body
    assert "Apple TV+" in body
    assert "Sci-Fi" in body
    assert "Returning Series" in body


def test_episodes_are_listed_with_derived_state(client):
    with session() as conn:
        sid = seed(conn)
    body = client.get(f"/series/{sid}").text
    assert "Cold Harbor" in body
    assert "S02E07" in body
    assert "in Plex" in body


def test_a_series_with_no_synced_episodes_explains_itself(client):
    with session() as conn:
        sid = seed(conn)
        conn.execute("DELETE FROM episodes WHERE series_id = ?", (sid,))
    assert "No episodes held for this series yet" in client.get(f"/series/{sid}").text


def test_the_pin_button_reflects_current_state(client):
    with session() as conn:
        sid = seed(conn, pinned=1)
    assert "Pinned" in client.get(f"/series/{sid}").text


def test_a_soft_match_is_called_out(client):
    with session() as conn:
        sid = seed(conn, match_confidence="soft")
    assert "soft match" in client.get(f"/series/{sid}").text


def test_an_unknown_series_is_a_404(client):
    assert client.get("/series/999999").status_code == 404


def test_the_library_links_to_it(client):
    with session() as conn:
        sid = seed(conn)
    assert f'href="/series/{sid}"' in client.get("/library").text


def test_external_links_appear_when_the_ids_exist(client):
    from app.config import save_settings
    from app.db import set_setting

    save_settings({"sonarr_url": "http://sonarr.lan:8989", "plex_url": "http://plex.lan:32400"})
    set_setting("plex_machine_id", "abc123")
    with session() as conn:
        sid = seed(conn, imdb_id="tt11280740", tmdb_id=95396,
                   title_slug="severance", plex_rating_key="4521")
    body = client.get(f"/series/{sid}").text
    assert "http://sonarr.lan:8989/series/severance" in body
    assert "abc123" in body
    assert "thetvdb.com/dereferrer/series/371980" in body
    assert "themoviedb.org/tv/95396" in body
    assert "imdb.com/title/tt11280740/" in body


def test_no_link_is_offered_when_the_id_is_missing(client):
    with session() as conn:
        sid = seed(conn, tvdb_id=None, tmdb_id=None, imdb_id=None)
    body = client.get(f"/series/{sid}").text
    assert "thetvdb.com" not in body
    assert "imdb.com" not in body


def test_a_plex_link_needs_the_machine_id_not_just_a_rating_key(client):
    """Before the first sync there is no machineIdentifier, and half a URL
    is worse than no link."""
    from app.config import save_settings

    save_settings({"plex_url": "http://plex.lan:32400"})
    with session() as conn:
        sid = seed(conn, plex_rating_key="4521")
    assert "/web/index.html" not in client.get(f"/series/{sid}").text


def test_a_missing_plex_link_explains_itself(client):
    """A button that is simply absent looks like a feature that was never
    built. This is the exact failure that shipped."""
    from app.config import save_settings

    save_settings({"plex_url": "http://plex.lan:32400"})
    with session() as conn:
        sid = seed(conn, plex_rating_key="4521")
    body = client.get(f"/series/{sid}").text
    assert "Test Plex in Settings" in body


def test_a_missing_sonarr_link_explains_itself(client):
    from app.config import save_settings

    save_settings({"sonarr_url": "http://sonarr.lan:8989"})
    with session() as conn:
        sid = seed(conn, sonarr_id=42, title_slug=None)
    assert "needs the series slug" in client.get(f"/series/{sid}").text


def test_nothing_is_explained_once_the_links_work(client):
    from app.config import save_settings
    from app.db import set_setting

    save_settings({"plex_url": "http://plex.lan:32400", "sonarr_url": "http://sonarr.lan:8989"})
    set_setting("plex_machine_id", "abc123")
    with session() as conn:
        sid = seed(conn, plex_rating_key="4521", sonarr_id=42, title_slug="severance")
    body = client.get(f"/series/{sid}").text
    assert "Test Plex in Settings" not in body
    assert "needs the series slug" not in body
