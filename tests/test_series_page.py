"""The per-series page, reachable from both the calendar and the library."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

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
    conn.execute("INSERT INTO genres (name) VALUES ('Sci-Fi')")
    gid = conn.execute("SELECT id FROM genres WHERE name = 'Sci-Fi'").fetchone()["id"]
    conn.execute("INSERT INTO series_genres (series_id, genre_id) VALUES (?, ?)", (sid, gid))
    return sid


@pytest.fixture
def client(db):
    with TestClient(app) as c:
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
    assert "No episodes in the synced window" in client.get(f"/series/{sid}").text


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
