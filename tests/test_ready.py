"""Ready to watch.

The calendar is built around anticipation. This is the other half: for
anyone who watches after a download rather than on transmission, "what can I
put on now" is the question they actually have.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import session, utcnow
from app.main import app
from app.repo import READY_DAYS


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


def seed(conn, user_id, *, title="Silo", episodes=(1,), days_ago=1, has_file=1,
         pinned=True, tvdb_id=None):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, tvdb_id, pinned, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (title, title.lower(), tvdb_id, int(pinned), now, now),
    )
    sid = int(cur.lastrowid)
    arrived = (
        (datetime.now(UTC) - timedelta(days=days_ago)).isoformat() if has_file else None
    )
    for number in episodes:
        conn.execute(
            "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
            "has_file, in_plex, monitored, arrived_at, updated_at) "
            "VALUES (?, 3, ?, ?, '2026-08-01T20:00:00+00:00', ?, 0, 1, ?, ?)",
            (sid, number, f"Episode {number}", has_file, arrived, now),
        )
    if pinned:
        conn.execute(
            "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
            (user_id, sid, now),
        )
    return sid


def test_a_recent_arrival_is_offered(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    body = client.get("/ready").text
    assert "Silo" in body
    assert "Episode 1" in body


def test_nothing_arrived_says_so(client):
    assert "Either you're caught up" in client.get("/ready").text


def test_something_that_has_not_arrived_is_not_offered(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, has_file=0)
    assert "Silo" not in client.get("/ready").text


def test_an_old_arrival_drops_off(client, admin_token):
    """It stays a shortlist rather than becoming an inventory."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, days_ago=READY_DAYS + 2)
    assert "Silo" not in client.get("/ready").text


def test_an_unpinned_series_is_not_offered(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, pinned=False)
    assert "Silo" not in client.get("/ready").text


def test_another_users_pin_does_not_appear(db, account):
    admin_tok, _ = account()
    bob_tok, bob = account("bob", "user")
    with session() as conn:
        seed(conn, bob, title="Silo")

    admin = TestClient(app)
    admin.cookies.set(auth.COOKIE, admin_tok)
    assert "Silo" not in admin.get("/ready").text

    bob_client = TestClient(app)
    bob_client.cookies.set(auth.COOKIE, bob_tok)
    assert "Silo" in bob_client.get("/ready").text


def test_episodes_are_grouped_into_one_decision_per_series(client, admin_token):
    """Four episodes of one show is one viewing decision, not four."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, episodes=(1, 2, 3, 4))
    body = client.get("/ready").text
    assert body.count('class="ready"') == 1
    assert "4 episodes" in body


def test_series_are_ordered_by_what_landed_most_recently(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, title="Older", days_ago=5, tvdb_id=1)
        seed(conn, user_id, title="Newer", days_ago=1, tvdb_id=2)
    body = client.get("/ready").text
    assert body.index("Newer") < body.index("Older")


def test_arrival_is_described_in_plain_language(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id, days_ago=1)
    assert "yesterday" in client.get("/ready").text


# ── Recency without an arrival stamp ──


def seed_no_stamp(conn, user_id, *, title="Street Cops", aired_days_ago=3, tvdb_id=None,
                  in_plex=1):
    """In Plex, but Pinnarr never watched the file appear — which is true of
    everything that was already there before it was installed."""
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, tvdb_id, pinned, created_at, updated_at) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (title, title.lower(), tvdb_id, now, now),
    )
    sid = int(cur.lastrowid)
    aired = (datetime.now(UTC) - timedelta(days=aired_days_ago)).isoformat()
    conn.execute(
        "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
        "has_file, in_plex, monitored, arrived_at, updated_at) "
        "VALUES (?, 2, 4, 'Episode 4', ?, 1, ?, 1, NULL, ?)",
        (sid, aired, in_plex, now),
    )
    conn.execute(
        "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
        (user_id, sid, now),
    )
    return sid


def test_something_in_plex_that_aired_recently_counts(client, admin_token):
    """The calendar shows these as in Plex; Ready must agree with it."""
    _, user_id = admin_token
    with session() as conn:
        seed_no_stamp(conn, user_id, aired_days_ago=3)
    assert "Street Cops" in client.get("/ready").text


def test_it_says_aired_rather_than_claiming_it_arrived(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed_no_stamp(conn, user_id, aired_days_ago=1)
    body = client.get("/ready").text
    assert "aired yesterday" in body
    assert "arrived yesterday" not in body


def test_a_real_arrival_stamp_still_wins(client, admin_token):
    """A late download of an old episode is news; its air date is not."""
    _, user_id = admin_token
    with session() as conn:
        sid = seed_no_stamp(conn, user_id, aired_days_ago=400)
        conn.execute(
            "UPDATE episodes SET arrived_at = ? WHERE series_id = ?",
            ((datetime.now(UTC) - timedelta(days=1)).isoformat(), sid),
        )
    body = client.get("/ready").text
    assert "Street Cops" in body
    assert "arrived yesterday" in body


def test_an_old_back_catalogue_stays_out(client, admin_token):
    """Line of Duty: in Plex, no arrival stamp, aired years ago."""
    _, user_id = admin_token
    with session() as conn:
        seed_no_stamp(conn, user_id, title="Line of Duty", aired_days_ago=2000)
    assert "Line of Duty" not in client.get("/ready").text


def test_a_file_for_something_not_yet_aired_is_not_ready(client, admin_token):
    """An early leak has a file and a future air date. It is not tonight's
    viewing, and dating it in the future would sort it above everything."""
    _, user_id = admin_token
    with session() as conn:
        seed_no_stamp(conn, user_id, aired_days_ago=-3)
    assert "Street Cops" not in client.get("/ready").text


# ── Runtime ──


def seed_runtime(conn, user_id, *, title="Silo", runtimes=(60,), tvdb_id=None):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, tvdb_id, pinned, created_at, updated_at) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (title, title.lower(), tvdb_id, now, now),
    )
    sid = int(cur.lastrowid)
    aired = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    for index, runtime in enumerate(runtimes, start=1):
        conn.execute(
            "INSERT INTO episodes (series_id, season, episode, title, air_date_utc, "
            "has_file, in_plex, monitored, runtime, updated_at) "
            "VALUES (?, 1, ?, ?, ?, 1, 1, 1, ?, ?)",
            (sid, index, f"Episode {index}", aired, runtime, now),
        )
    conn.execute(
        "INSERT INTO pins (user_id, series_id, pinned_at) VALUES (?, ?, ?)",
        (user_id, sid, now),
    )
    return sid


def test_a_group_shows_how_long_it_would_take(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed_runtime(conn, user_id, runtimes=(60, 45, 60))
    assert "2h 45m" in client.get("/ready").text


def test_the_page_totals_everything_waiting(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed_runtime(conn, user_id, runtimes=(60,), tvdb_id=1)
        seed_runtime(conn, user_id, title="Andor", runtimes=(30,), tvdb_id=2)
    assert "1h 30m" in client.get("/ready").text


def test_a_null_runtime_does_not_break_the_total(client, admin_token):
    """Plenty of episodes have no runtime, and Jinja's sum filter cannot add
    None to an integer."""
    _, user_id = admin_token
    with session() as conn:
        seed_runtime(conn, user_id, runtimes=(60, None))
    assert client.get("/ready").status_code == 200


def test_filtering_by_what_fits_in_an_hour(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed_runtime(conn, user_id, title="Short", runtimes=(30,), tvdb_id=1)
        seed_runtime(conn, user_id, title="Long", runtimes=(90,), tvdb_id=2)
    body = client.get("/ready?fits=60").text
    assert "Short" in body
    assert "Long" not in body


def test_an_episode_with_no_runtime_is_excluded_when_filtering(client, admin_token):
    """It might be four hours. Offering it under "fits in 30 minutes" would
    be a guess dressed as an answer."""
    _, user_id = admin_token
    with session() as conn:
        seed_runtime(conn, user_id, runtimes=(None,))
    assert "Silo" not in client.get("/ready?fits=30").text


def test_a_nonsense_filter_falls_back_to_everything(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed_runtime(conn, user_id)
    assert "Silo" in client.get("/ready?fits=banana").text


def test_filtering_to_nothing_offers_a_way_back(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed_runtime(conn, user_id, runtimes=(90,))
    assert "Show everything" in client.get("/ready?fits=30").text


def test_a_huge_group_is_capped_with_a_way_to_see_the_rest(client, admin_token):
    """A whole imported series is one arrival; thirty-seven rows of it buries
    everything else on the page."""
    _, user_id = admin_token
    with session() as conn:
        seed_runtime(conn, user_id, runtimes=[30] * 20)
    body = client.get("/ready").text
    assert "14 more…" in body
    assert "Episode 20" in body   # still reachable, just folded


def test_a_small_group_gets_no_expander(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed_runtime(conn, user_id, runtimes=(30, 30))
    assert "more…" not in client.get("/ready").text
