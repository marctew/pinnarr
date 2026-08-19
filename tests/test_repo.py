"""Identity resolution and upsert behaviour.

The scenarios that matter are the ones where the same show arrives from two
sources and has to land on one row.
"""

from app.clients.plex import PlexShow
from app.clients.sonarr import SonarrEpisode, SonarrSeries
from app.repo import (
    bulk_pin,
    latest_bulk_batch,
    replace_genres,
    resolve_series_id,
    set_pinned,
    undo_bulk_pin,
    upsert_episode,
    upsert_from_plex,
    upsert_from_sonarr,
)


def plex_show(**kw) -> PlexShow:
    defaults = {
        "rating_key": "1234", "section_id": 2, "title": "Severance", "sort_title": "Severance",
        "year": 2022, "summary": "Mark leads a team.", "thumb": "/library/metadata/1234/thumb",
        "tvdb_id": 371980, "genres": ["Drama", "Sci-Fi"],
    }
    return PlexShow(**{**defaults, **kw})


def sonarr_series(**kw) -> SonarrSeries:
    defaults = {
        "sonarr_id": 7, "tvdb_id": 371980, "tmdb_id": 95396, "imdb_id": "tt11280740",
        "title": "Severance", "sort_title": "severance", "year": 2022, "status": "continuing",
        "network": "Apple TV+", "overview": "Mark leads a team.", "monitored": True,
        "next_airing": "2026-08-22T01:00:00Z", "previous_airing": "2026-08-15T01:00:00Z",
        "latest_season": 2, "seasons": [1, 2],
    }
    return SonarrSeries(**{**defaults, **kw})


def sonarr_episode(**kw) -> SonarrEpisode:
    defaults = {
        "sonarr_episode_id": 555, "sonarr_series_id": 7, "tvdb_id": 371980, "season": 2,
        "episode": 7, "title": "Cold Harbor", "air_date_utc": "2026-08-22T01:00:00Z",
        "runtime": 50, "monitored": True, "has_file": False,
    }
    return SonarrEpisode(**{**defaults, **kw})


def test_plex_then_sonarr_lands_on_one_row(db):
    """The core join: same TVDB id from two sources must not create two rows."""
    with db() as conn:
        plex_id = upsert_from_plex(conn, plex_show())
        sonarr_id = upsert_from_sonarr(conn, sonarr_series())
        assert plex_id == sonarr_id

        row = conn.execute("SELECT * FROM series").fetchone()
        assert conn.execute("SELECT COUNT(*) c FROM series").fetchone()["c"] == 1
        # Each source contributed what it owns.
        assert row["plex_rating_key"] == "1234"
        assert row["sonarr_id"] == 7
        assert row["network"] == "Apple TV+"
        assert row["in_plex"] == 1
        assert row["in_sonarr"] == 1


def test_sonarr_first_then_plex(db):
    """Order must not matter — Sonarr may know a show before it's in Plex."""
    with db() as conn:
        a = upsert_from_sonarr(conn, sonarr_series())
        b = upsert_from_plex(conn, plex_show())
        assert a == b
        assert conn.execute("SELECT COUNT(*) c FROM series").fetchone()["c"] == 1


def test_series_only_in_sonarr_still_gets_a_row(db):
    """You may want to pin a show whose first episode hasn't landed yet."""
    with db() as conn:
        upsert_from_sonarr(conn, sonarr_series())
        row = conn.execute("SELECT in_plex, in_sonarr FROM series").fetchone()
        assert row["in_plex"] == 0
        assert row["in_sonarr"] == 1


def test_no_external_id_falls_back_to_title_and_year(db):
    with db() as conn:
        upsert_from_plex(conn, plex_show(tvdb_id=None, tmdb_id=None, imdb_id=None))
        series_id, confidence = resolve_series_id(conn, title="severance", year=2022)
        assert series_id is not None
        assert confidence == "soft"


def test_soft_match_is_recorded(db):
    """A title-only match must be visible, not silently treated as certain."""
    with db() as conn:
        upsert_from_plex(conn, plex_show(tvdb_id=None))
        upsert_from_plex(conn, plex_show(rating_key="9999", tvdb_id=None))
        row = conn.execute("SELECT match_confidence FROM series").fetchone()
        assert row["match_confidence"] == "soft"


def test_plex_does_not_clobber_sonarr_network(db):
    with db() as conn:
        upsert_from_sonarr(conn, sonarr_series())
        upsert_from_plex(conn, plex_show())
        row = conn.execute("SELECT network FROM series").fetchone()
        assert row["network"] == "Apple TV+"


def test_genres_are_replaced_not_duplicated(db):
    with db() as conn:
        sid = upsert_from_plex(conn, plex_show())
        replace_genres(conn, sid, ["Drama", "Thriller"])
        names = {
            r["name"]
            for r in conn.execute(
                "SELECT g.name FROM genres g JOIN series_genres sg ON sg.genre_id = g.id "
                "WHERE sg.series_id = ?", (sid,)
            )
        }
        assert names == {"Drama", "Thriller"}


def test_arrived_at_is_stamped_once_and_never_moved(db):
    """A quality upgrade must not look like a fresh arrival."""
    with db() as conn:
        sid = upsert_from_sonarr(conn, sonarr_series())
        upsert_episode(conn, sid, sonarr_episode(has_file=False))
        assert conn.execute("SELECT arrived_at FROM episodes").fetchone()["arrived_at"] is None

        upsert_episode(conn, sid, sonarr_episode(has_file=True))
        first = conn.execute("SELECT arrived_at FROM episodes").fetchone()["arrived_at"]
        assert first is not None

        # Re-import at better quality.
        upsert_episode(conn, sid, sonarr_episode(has_file=True, title="Cold Harbor (1080p)"))
        second = conn.execute("SELECT arrived_at FROM episodes").fetchone()["arrived_at"]
        assert second == first


def test_episode_upsert_is_idempotent(db):
    with db() as conn:
        sid = upsert_from_sonarr(conn, sonarr_series())
        upsert_episode(conn, sid, sonarr_episode())
        upsert_episode(conn, sid, sonarr_episode())
        assert conn.execute("SELECT COUNT(*) c FROM episodes").fetchone()["c"] == 1


def test_bulk_pin_and_undo(db, admin_token):
    _, user = admin_token
    with db() as conn:
        ids = [
            upsert_from_plex(conn, plex_show(rating_key=str(i), tvdb_id=1000 + i, title=f"Show {i}"))
            for i in range(5)
        ]
        count, batch = bulk_pin(conn, user, ids)
        assert count == 5
        assert conn.execute("SELECT COUNT(*) c FROM series WHERE pinned = 1").fetchone()["c"] == 5
        assert latest_bulk_batch(conn, user) == batch

        assert undo_bulk_pin(conn, user, batch) == 5
        assert conn.execute("SELECT COUNT(*) c FROM series WHERE pinned = 1").fetchone()["c"] == 0


def test_bulk_pin_skips_already_pinned_so_undo_does_not_unpin_them(db, admin_token):
    _, user = admin_token
    """Undo must only reverse what that bulk action actually did."""
    with db() as conn:
        ids = [
            upsert_from_plex(conn, plex_show(rating_key=str(i), tvdb_id=2000 + i, title=f"S{i}"))
            for i in range(3)
        ]
        set_pinned(conn, user, ids[0], True)  # pinned by hand, earlier

        count, batch = bulk_pin(conn, user, ids)
        assert count == 2

        undo_bulk_pin(conn, user, batch)
        still_pinned = conn.execute(
            "SELECT id FROM series WHERE pinned = 1"
        ).fetchall()
        assert [r["id"] for r in still_pinned] == [ids[0]]
