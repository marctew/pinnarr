"""Faceted library browsing (SPEC §11).

Built against a real library of ~2000 series, so the assumptions that matter
are about narrowing and paging, not about rendering forty posters.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import session, utcnow
from app.main import app
from app.repo import (
    PAGE_SIZE,
    LibraryFilter,
    count_series,
    facet_counts,
    matching_ids,
    query_series,
)


def add(conn, title, **kw):
    now = utcnow()
    fields = {
        "tvdb_id": None, "plex_section_id": 2, "sort_title": title.lower(),
        "year": 2020, "network": "BBC", "sonarr_status": "continuing",
        "outlook": "dated", "pinned": 0, "last_watched_at": None, "next_airing": None,
    }
    fields.update(kw)
    cols = ", ".join(["title", "created_at", "updated_at", *fields])
    marks = ", ".join(["?"] * (3 + len(fields)))
    cur = conn.execute(
        f"INSERT INTO series ({cols}) VALUES ({marks})",
        [title, now, now, *fields.values()],
    )
    return int(cur.lastrowid)


def genre(conn, series_id, name):
    conn.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (name,))
    gid = conn.execute("SELECT id FROM genres WHERE name = ?", (name,)).fetchone()["id"]
    conn.execute(
        "INSERT OR IGNORE INTO series_genres (series_id, genre_id) VALUES (?, ?)",
        (series_id, gid),
    )


@pytest.fixture
def library(db):
    with db() as conn:
        a = add(conn, "Severance", outlook="dated", network="Apple", sonarr_status="continuing")
        b = add(conn, "Dark", outlook="ended", network="Netflix", sonarr_status="ended")
        c = add(conn, "The Bear", outlook="between_seasons", network="FX", plex_section_id=5)
        genre(conn, a, "Sci-Fi")
        genre(conn, b, "Sci-Fi")
        genre(conn, c, "Comedy")
    return db


def test_no_filter_returns_everything(library):
    with library() as conn:
        assert count_series(conn, LibraryFilter()) == 3


def test_search_is_a_substring_match(library):
    with library() as conn:
        rows = query_series(conn, LibraryFilter(search="bear"))
    assert [r["title"] for r in rows] == ["The Bear"]


def test_values_within_a_facet_are_ored(library):
    with library() as conn:
        n = count_series(conn, LibraryFilter(outlooks=("dated", "ended")))
    assert n == 2


def test_facets_are_anded_together(library):
    with library() as conn:
        assert count_series(conn, LibraryFilter(outlooks=("dated",), networks=("Netflix",))) == 0
        assert count_series(conn, LibraryFilter(outlooks=("dated",), networks=("Apple",))) == 1


def test_genre_filtering_goes_through_the_join(library):
    with library() as conn:
        rows = query_series(conn, LibraryFilter(genres=("Sci-Fi",)))
    assert {r["title"] for r in rows} == {"Severance", "Dark"}


def test_a_facets_own_values_are_counted_without_it_applied(library):
    """Counts answer "what if I ticked this too", so a facet must not
    constrain its own counts — otherwise every unticked value reads zero."""
    with library() as conn:
        counts = facet_counts(conn, LibraryFilter(outlooks=("dated",)))
    outlooks = {c["value"]: c["count"] for c in counts["outlooks"]}
    assert outlooks == {"dated": 1, "ended": 1, "between_seasons": 1}


def test_other_facets_do_constrain_the_counts(library):
    with library() as conn:
        counts = facet_counts(conn, LibraryFilter(networks=("Apple",)))
    assert {c["value"]: c["count"] for c in counts["outlooks"]} == {"dated": 1}


def test_sorting_by_title(library):
    with library() as conn:
        rows = query_series(conn, LibraryFilter(sort="title"))
    assert [r["title"] for r in rows] == ["Dark", "Severance", "The Bear"]


def test_sorting_by_outlook_follows_the_spec_ladder(library):
    with library() as conn:
        rows = query_series(conn, LibraryFilter(sort="outlook"))
    assert [r["outlook"] for r in rows] == ["dated", "between_seasons", "ended"]


def test_undated_shows_sort_last_by_next_airing(db):
    with db() as conn:
        add(conn, "No date")
        add(conn, "Soon", next_airing="2026-09-01T20:00:00+00:00")
    with db() as conn:
        rows = query_series(conn, LibraryFilter(sort="next"))
    assert [r["title"] for r in rows] == ["Soon", "No date"]


def test_paging_at_library_scale(db):
    with db() as conn:
        for i in range(PAGE_SIZE + 20):
            add(conn, f"Show {i:03d}")
    with db() as conn:
        assert count_series(conn, LibraryFilter()) == PAGE_SIZE + 20
        assert len(query_series(conn, LibraryFilter(sort="title"))) == PAGE_SIZE
        assert len(query_series(conn, LibraryFilter(sort="title", page=2))) == 20


def test_matching_ids_ignores_paging(db):
    with db() as conn:
        for i in range(PAGE_SIZE + 20):
            add(conn, f"Show {i:03d}")
    with db() as conn:
        assert len(matching_ids(conn, LibraryFilter())) == PAGE_SIZE + 20


# ── Routes ──


@pytest.fixture
def client(library, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


def test_library_page_renders(client):
    r = client.get("/library")
    assert r.status_code == 200
    assert "Severance" in r.text


def test_filters_survive_in_the_url(client):
    r = client.get("/library?outlook=ended&sort=title")
    assert "Dark" in r.text
    assert "Severance" not in r.text.split('class="grid"')[1]


def test_a_nonsense_sort_falls_back_rather_than_500ing(client):
    assert client.get("/library?sort=; DROP TABLE series&page=x").status_code == 200


def test_pin_and_unpin_round_trip(client):
    with session() as conn:
        sid = conn.execute("SELECT id FROM series WHERE title = 'Dark'").fetchone()["id"]

    assert client.post(f"/api/series/{sid}/pin").json()["pinned"] is True
    body = client.post(f"/api/series/{sid}/unpin").json()
    assert body["pinned"] is False
    assert body["pinned_total"] == 0


def test_pinning_something_that_does_not_exist_is_a_404(client):
    assert client.post("/api/series/999999/pin").status_code == 404


def test_bulk_pin_re_runs_the_filter_server_side(client):
    r = client.post("/api/series/bulk-pin?outlook=dated")
    assert r.json()["pinned"] == 1
    assert r.json()["pinned_total"] == 1


def test_bulk_pin_does_not_double_count_already_pinned(client):
    client.post("/api/series/bulk-pin?outlook=dated")
    second = client.post("/api/series/bulk-pin?outlook=dated").json()
    assert second["pinned"] == 0


def test_undo_reverses_the_last_bulk_pin_only(client):
    client.post("/api/series/bulk-pin?outlook=dated")
    client.post("/api/series/bulk-pin?outlook=ended")
    assert client.post("/api/series/bulk-undo").json()["undone"] == 1
    assert client.post("/api/series/bulk-undo").json()["undone"] == 1


def test_a_missing_poster_serves_a_placeholder_not_a_broken_image(client):
    with session() as conn:
        sid = conn.execute("SELECT id FROM series WHERE title = 'Dark'").fetchone()["id"]
    r = client.get(f"/poster/{sid}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg")


def test_facets_use_friendly_names_not_stored_values(client):
    body = client.get("/library").text
    assert ">On hiatus<" in body        # between_seasons
    assert ">Continuing<" in body       # sonarr_status
    assert ">between_seasons<" not in body


def test_a_plex_section_shows_its_name_not_its_id(client):
    with session() as conn:
        conn.execute(
            "INSERT INTO plex_sections (id, title, type, agent, seen_at) "
            "VALUES (2, 'TV Shows', 'show', 'tv.plex.agents.series', '2026-08-19T00:00:00+00:00')"
        )
    assert ">TV Shows<" in client.get("/library").text


def test_an_unknown_section_id_still_renders(client):
    """Section names arrive with the next Plex sync; the facet must not break
    before then."""
    assert client.get("/library").status_code == 200


def test_the_poster_links_to_the_show_and_the_pin_is_its_own_control(client):
    body = client.get("/library").text
    assert 'class="poster" href="/series/' in body
    assert 'class="pin" onclick="togglePin(' in body


def test_the_pin_sits_inside_the_thumbnail_not_the_card(client):
    """Anchored to the card, the pin drifts onto the title as soon as one
    wraps to two lines — which most of them do."""
    body = client.get("/library").text
    thumb = body.split('<div class="thumb">')[1].split("</div>")[0]
    assert 'class="pin"' in thumb


def test_the_pin_button_still_announces_itself_without_visible_text(client):
    body = client.get("/library").text
    assert 'aria-pressed=' in body
    assert '<span class="sr">' in body
