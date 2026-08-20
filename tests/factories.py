"""Row builders shared by the test modules.

Twenty-seven near-identical `seed`/`add`/`add_series` helpers had grown up
across the suite, each writing out the same INSERT with slightly different
defaults. The defaults are the part worth keeping local to a file — they say
what that file is about. The SQL is not, and a column added to `series` meant
finding every copy.

So the SQL lives here and the per-file helpers stay, as two or three lines
naming their own defaults.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from app.db import utcnow


def iso(*, days: float = 0, hours: float = 0) -> str:
    """An ISO timestamp offset from now. Negative is the past."""
    return (datetime.now(UTC) + timedelta(days=days, hours=hours)).isoformat()


def make_series(conn: sqlite3.Connection, title: str = "Silo", *,
                pinned_by: int | None = None, **columns) -> int:
    """Insert a series and return its id.

    Any column of `series` can be passed by name; `sort_title` and the
    timestamps default so no caller has to think about them.
    """
    now = utcnow()
    fields: dict[str, object] = {
        "sort_title": title.lower(), "created_at": now, "updated_at": now
    }
    fields.update(columns)
    names = ", ".join(["title", *fields])
    marks = ", ".join(["?"] * (1 + len(fields)))
    cur = conn.execute(
        f"INSERT INTO series ({names}) VALUES ({marks})", [title, *fields.values()]
    )
    series_id = int(cur.lastrowid)
    if pinned_by:
        pin(conn, pinned_by, series_id)
    return series_id


def pin(conn: sqlite3.Connection, user_id: int, series_id: int, *,
        pinned_at: str | None = None, notify: int = 1) -> None:
    """Pin for one user, and set the denormalised flag the sync jobs read.

    Both halves, always. A pin row without `series.pinned` is a state the app
    itself never produces, and a test that starts there proves nothing.
    """
    conn.execute(
        "INSERT INTO pins (user_id, series_id, pinned_at, notify) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (user_id, series_id) DO NOTHING",
        (user_id, series_id, pinned_at or utcnow(), notify),
    )
    conn.execute("UPDATE series SET pinned = 1 WHERE id = ?", (series_id,))


def make_episode(conn: sqlite3.Connection, series_id: int, *, season: int = 1,
                 episode: int = 1, **columns) -> int:
    """Insert an episode and return its id. Monitored by default, like Sonarr."""
    fields: dict[str, object] = {
        "title": f"Episode {episode}",
        "monitored": 1,
        "updated_at": utcnow(),
    }
    fields.update(columns)
    names = ", ".join(["series_id", "season", "episode", *fields])
    marks = ", ".join(["?"] * (3 + len(fields)))
    cur = conn.execute(
        f"INSERT INTO episodes ({names}) VALUES ({marks})",
        [series_id, season, episode, *fields.values()],
    )
    return int(cur.lastrowid)


def watch(conn: sqlite3.Connection, user_id: int, episode_id: int, *,
          watched_at: str | None = None, source: str = "plex") -> None:
    conn.execute(
        "INSERT INTO episode_watches (user_id, episode_id, watched_at, source) "
        "VALUES (?, ?, ?, ?)",
        (user_id, episode_id, watched_at or utcnow(), source),
    )
