"""When an episode counts as having arrived.

arrived_at drives Ready to Watch and every notification, and it means "we
watched this become available" — not "we first saw it with a file". Those are
the same thing for a series being tracked and wildly different for one being
imported wholesale.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.clients.sonarr import SonarrEpisode
from app.db import session, utcnow
from app.repo import upsert_episode


def episode(number=1, *, has_file=False, aired_days_ago=1000, season=1):
    air = datetime.now(UTC) - timedelta(days=aired_days_ago)
    return SonarrEpisode(
        sonarr_episode_id=1000 + number,
        sonarr_series_id=7,
        tvdb_id=None,
        season=season,
        episode=number,
        title=f"Episode {number}",
        air_date_utc=air.isoformat(),
        runtime=60,
        monitored=True,
        has_file=has_file,
    )


def add_series(conn):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, created_at, updated_at) "
        "VALUES ('Line of Duty', 'line of duty', ?, ?)",
        (now, now),
    )
    return int(cur.lastrowid)


def arrived_of(conn, series_id, number, season=1):
    return conn.execute(
        "SELECT arrived_at FROM episodes WHERE series_id = ? AND season = ? AND episode = ?",
        (series_id, season, number),
    ).fetchone()["arrived_at"]


def test_a_back_catalogue_does_not_all_arrive_today(db):
    """The bug: pulling six seasons stamped every episode as arriving now,
    and Ready to Watch believed it."""
    with session() as conn:
        sid = add_series(conn)
        for number in range(1, 6):
            upsert_episode(conn, sid, episode(number, has_file=True, aired_days_ago=2000))
        for number in range(1, 6):
            assert arrived_of(conn, sid, number) is None


def test_watching_a_file_appear_is_an_arrival(db):
    with session() as conn:
        sid = add_series(conn)
        upsert_episode(conn, sid, episode(1, has_file=False))
        assert arrived_of(conn, sid, 1) is None

        upsert_episode(conn, sid, episode(1, has_file=True))
        assert arrived_of(conn, sid, 1) is not None


def test_a_first_sighting_of_something_that_just_aired_counts(db):
    """A genuinely new episode grabbed between syncs is a real arrival."""
    with session() as conn:
        sid = add_series(conn)
        upsert_episode(conn, sid, episode(1, has_file=True, aired_days_ago=0))
        assert arrived_of(conn, sid, 1) is not None


def test_an_old_episode_seen_again_is_still_not_an_arrival(db):
    """The row already had a file, so nothing was observed to change — and it
    must not get stamped on the next sync either."""
    with session() as conn:
        sid = add_series(conn)
        upsert_episode(conn, sid, episode(1, has_file=True, aired_days_ago=2000))
        upsert_episode(conn, sid, episode(1, has_file=True, aired_days_ago=2000))
        assert arrived_of(conn, sid, 1) is None


def test_an_upgrade_does_not_move_the_timestamp(db):
    with session() as conn:
        sid = add_series(conn)
        upsert_episode(conn, sid, episode(1, has_file=False))
        upsert_episode(conn, sid, episode(1, has_file=True))
        first = arrived_of(conn, sid, 1)

        upsert_episode(conn, sid, episode(1, has_file=True))
        assert arrived_of(conn, sid, 1) == first


def test_an_undated_episode_is_never_assumed_to_be_new(db):
    with session() as conn:
        sid = add_series(conn)
        ep = episode(1, has_file=True)
        ep.air_date_utc = None
        upsert_episode(conn, sid, ep)
        assert arrived_of(conn, sid, 1) is None


def test_a_nonsense_air_date_does_not_crash_the_sync(db):
    with session() as conn:
        sid = add_series(conn)
        ep = episode(1, has_file=True)
        ep.air_date_utc = "not a date"
        upsert_episode(conn, sid, ep)
        assert arrived_of(conn, sid, 1) is None
