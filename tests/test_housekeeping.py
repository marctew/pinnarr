"""Nightly tidying.

Everything here is unbounded growth that nobody notices until the box has
been running unattended for a year.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from app import auth, media
from app.db import session, utcnow
from app.jobs import housekeeping as hk
from tests.factories import make_series


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(hk, "cache_dir", lambda: tmp_path)
    return tmp_path


def add_series(conn, title="Silo"):
    return make_series(conn, title)


def test_expired_sessions_are_collected(db):
    with session() as conn:
        user_id = auth.create_user(conn, "marc", "password123", "admin")
        live = auth.start_session(conn, user_id)
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            ("stale", user_id, utcnow(),
             (datetime.now(UTC) - timedelta(days=1)).isoformat()),
        )
        assert auth.purge_expired(conn) == 1
        assert auth.user_for_token(conn, live) is not None
        assert auth.user_for_token(conn, "stale") is None


def test_only_the_most_recent_runs_are_kept(db):
    with session() as conn:
        for _ in range(hk.KEEP_RUNS + 25):
            conn.execute(
                "INSERT INTO sync_log (job, started_at, status) VALUES ('plex_library', ?, 'ok')",
                (utcnow(),),
            )
    assert hk.prune_sync_log() == 25
    with session() as conn:
        assert conn.execute("SELECT count(*) AS n FROM sync_log").fetchone()["n"] == hk.KEEP_RUNS


def test_each_job_keeps_its_own_history(db):
    """Pruning globally would let a chatty job evict a quiet one's only run."""
    with session() as conn:
        for _ in range(hk.KEEP_RUNS + 10):
            conn.execute(
                "INSERT INTO sync_log (job, started_at, status) VALUES ('sonarr_calendar', ?, 'ok')",
                (utcnow(),),
            )
        conn.execute(
            "INSERT INTO sync_log (job, started_at, status) VALUES ('tmdb_status', ?, 'ok')",
            (utcnow(),),
        )
    hk.prune_sync_log()
    with session() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM sync_log WHERE job = 'tmdb_status'"
        ).fetchone()["n"] == 1


def test_art_for_a_deleted_series_is_dropped(db, cache):
    with session() as conn:
        live_id = add_series(conn)
    keep = cache / f"{live_id}-abc"
    orphan = cache / "99999-def"
    keep.write_bytes(b"art")
    orphan.write_bytes(b"art")

    assert hk.prune_posters() == 1
    assert keep.exists()
    assert not orphan.exists()


def test_stale_art_is_dropped_even_for_a_live_series(db, cache):
    with session() as conn:
        live_id = add_series(conn)
    old = cache / f"{live_id}-old"
    old.write_bytes(b"art")
    ancient = time.time() - (hk.POSTER_MAX_AGE_DAYS + 1) * 86400
    import os

    os.utime(old, (ancient, ancient))

    assert hk.prune_posters() == 1
    assert not old.exists()


def test_a_torn_write_is_cleaned_up(db, cache):
    with session() as conn:
        live_id = add_series(conn)
    partial = cache / f"{live_id}-abc.part"
    partial.write_bytes(b"half")
    assert hk.prune_posters() == 1
    assert not partial.exists()


async def test_the_job_reports_what_it_did(db, cache):
    with session() as conn:
        add_series(conn)
    detail = await hk.housekeeping()
    assert "expired session" in detail
    assert "cached poster" in detail


async def test_the_job_is_recorded_like_any_other(db, cache):
    await hk.housekeeping()
    with session() as conn:
        row = conn.execute(
            "SELECT status FROM sync_log WHERE job = 'housekeeping'"
        ).fetchone()
    assert row["status"] == "ok"
