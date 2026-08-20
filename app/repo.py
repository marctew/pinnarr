"""Write-side data access: identity resolution and upserts.

The interesting problem here is series identity. The same show arrives from
Plex (with a GUID), Sonarr (with a tvdbId) and TMDB (with its own id), and
all three have to land on one row. TVDB id is the primary key we trust;
everything else is a fallback with decreasing confidence.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.clients.plex import PlexShow
from app.clients.sonarr import SonarrEpisode, SonarrSeries
from app.db import utcnow

log = logging.getLogger(__name__)


def _normalise(title: str) -> str:
    """Crude title normalisation for last-resort matching."""
    return "".join(c for c in title.lower() if c.isalnum())


def resolve_series_id(
    conn: sqlite3.Connection,
    *,
    tvdb_id: int | None = None,
    tmdb_id: int | None = None,
    imdb_id: str | None = None,
    plex_rating_key: str | None = None,
    sonarr_id: int | None = None,
    title: str | None = None,
    year: int | None = None,
) -> tuple[int | None, str]:
    """Find an existing series row. Returns (id, confidence).

    Tried in descending order of trust. The title+year fallback is recorded
    as a soft match so the UI can flag it rather than silently pretending
    two different shows are the same one.
    """
    if tvdb_id:
        row = conn.execute("SELECT id FROM series WHERE tvdb_id = ?", (tvdb_id,)).fetchone()
        if row:
            return row["id"], "exact"

    if plex_rating_key:
        row = conn.execute(
            "SELECT id FROM series WHERE plex_rating_key = ?", (plex_rating_key,)
        ).fetchone()
        if row:
            return row["id"], "exact"

    if sonarr_id:
        row = conn.execute("SELECT id FROM series WHERE sonarr_id = ?", (sonarr_id,)).fetchone()
        if row:
            return row["id"], "exact"

    if tmdb_id:
        row = conn.execute("SELECT id FROM series WHERE tmdb_id = ?", (tmdb_id,)).fetchone()
        if row:
            return row["id"], "exact"

    if imdb_id:
        row = conn.execute("SELECT id FROM series WHERE imdb_id = ?", (imdb_id,)).fetchone()
        if row:
            return row["id"], "exact"

    # Last resort. Anime and shows with regional retitles land here.
    if title:
        candidates = conn.execute(
            "SELECT id, title, year FROM series WHERE year IS ? OR year = ?", (year, year)
        ).fetchall()
        target = _normalise(title)
        for row in candidates:
            if _normalise(row["title"]) == target:
                return row["id"], "soft"

    return None, "exact"


def _apply(
    conn: sqlite3.Connection, series_id: int, fields: dict[str, Any], *, overwrite: bool
) -> None:
    """Update a series row.

    With overwrite=False, only fills columns that are currently NULL/empty —
    so a later source can enrich a row without clobbering better data from
    an earlier one.
    """
    if not fields:
        return

    if not overwrite:
        current = conn.execute("SELECT * FROM series WHERE id = ?", (series_id,)).fetchone()
        fields = {
            k: v
            for k, v in fields.items()
            if v not in (None, "") and current[k] in (None, "")
        }
        if not fields:
            return

    fields["updated_at"] = utcnow()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE series SET {assignments} WHERE id = ?", (*fields.values(), series_id)
    )


def upsert_from_plex(conn: sqlite3.Connection, show: PlexShow) -> int:
    """Create or update a series row from a Plex library entry."""
    series_id, confidence = resolve_series_id(
        conn,
        tvdb_id=show.tvdb_id,
        tmdb_id=show.tmdb_id,
        imdb_id=show.imdb_id,
        plex_rating_key=show.rating_key,
        title=show.title,
        year=show.year,
    )

    core = {
        "tvdb_id": show.tvdb_id,
        "tmdb_id": show.tmdb_id,
        "imdb_id": show.imdb_id,
        "plex_rating_key": show.rating_key,
        "plex_section_id": show.section_id,
        "poster_url": show.thumb,
        "plex_guid": show.plex_guid,
        "in_plex": 1,
    }

    if series_id is None:
        now = utcnow()
        cur = conn.execute(
            """
            INSERT INTO series (
                tvdb_id, tmdb_id, imdb_id, plex_rating_key, plex_section_id,
                title, sort_title, year, overview, poster_url,
                in_plex, match_confidence, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?,?)
            """,
            (
                show.tvdb_id, show.tmdb_id, show.imdb_id, show.rating_key, show.section_id,
                show.title, show.sort_title or show.title, show.year, show.summary,
                show.thumb, confidence, now, now,
            ),
        )
        series_id = int(cur.lastrowid or 0)
    else:
        # Plex is authoritative for the things Plex owns.
        _apply(conn, series_id, core, overwrite=True)
        _apply(
            conn,
            series_id,
            {
                "title": show.title,
                "sort_title": show.sort_title,
                "year": show.year,
                "overview": show.summary,
            },
            overwrite=False,
        )
        if confidence == "soft":
            _apply(conn, series_id, {"match_confidence": "soft"}, overwrite=True)

    replace_genres(conn, series_id, show.genres)
    return series_id


def upsert_from_sonarr(conn: sqlite3.Connection, s: SonarrSeries) -> int:
    """Create or update a series row from Sonarr.

    Series Sonarr manages but Plex doesn't have yet still get a row — you
    may well want to pin a show whose first episode hasn't landed.
    """
    series_id, confidence = resolve_series_id(
        conn,
        tvdb_id=s.tvdb_id,
        tmdb_id=s.tmdb_id,
        imdb_id=s.imdb_id,
        sonarr_id=s.sonarr_id,
        title=s.title,
        year=s.year,
    )

    owned_by_sonarr = {
        "sonarr_id": s.sonarr_id,
        "title_slug": s.title_slug,
        "remote_poster": s.poster_url,
        "sonarr_status": s.status,
        "network": s.network,
        "next_airing": s.next_airing,
        "previous_airing": s.previous_airing,
        "latest_season": s.latest_season,
        "in_sonarr": 1,
    }

    if series_id is None:
        now = utcnow()
        cur = conn.execute(
            """
            INSERT INTO series (
                tvdb_id, tmdb_id, imdb_id, sonarr_id, title, sort_title, year,
                network, overview, sonarr_status, next_airing, previous_airing,
                latest_season, in_sonarr, match_confidence, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)
            """,
            (
                s.tvdb_id, s.tmdb_id, s.imdb_id, s.sonarr_id, s.title,
                s.sort_title or s.title, s.year, s.network, s.overview, s.status,
                s.next_airing, s.previous_airing, s.latest_season, confidence, now, now,
            ),
        )
        return int(cur.lastrowid or 0)

    _apply(conn, series_id, owned_by_sonarr, overwrite=True)
    _apply(
        conn,
        series_id,
        {
            "tvdb_id": s.tvdb_id,
            "tmdb_id": s.tmdb_id,
            "imdb_id": s.imdb_id,
            "title": s.title,
            "sort_title": s.sort_title,
            "year": s.year,
            "overview": s.overview,
        },
        overwrite=False,
    )
    return series_id


def replace_genres(conn: sqlite3.Connection, series_id: int, names: list[str]) -> None:
    if not names:
        return
    genre_ids = []
    for name in {n.strip() for n in names if n and n.strip()}:
        conn.execute("INSERT OR IGNORE INTO genres (name) VALUES (?)", (name,))
        row = conn.execute("SELECT id FROM genres WHERE name = ?", (name,)).fetchone()
        if row:
            genre_ids.append(row["id"])

    conn.execute("DELETE FROM series_genres WHERE series_id = ?", (series_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO series_genres (series_id, genre_id) VALUES (?, ?)",
        [(series_id, g) for g in genre_ids],
    )


#: How recently an episode must have aired for a first sighting with a file
#: to be believable as an arrival.
ARRIVAL_GRACE_HOURS = 48


def arrival_is_plausible(air_date_utc: str | None) -> bool:
    """Whether seeing a file now can honestly be called an arrival.

    A back catalogue nobody had looked at before is not news. Only something
    that aired in the last couple of days could plausibly have landed in the
    window since we last checked.
    """
    if not air_date_utc:
        return False
    try:
        air = datetime.fromisoformat(air_date_utc)
    except ValueError:
        return False
    if air.tzinfo is None:
        air = air.replace(tzinfo=UTC)
    return air >= datetime.now(UTC) - timedelta(hours=ARRIVAL_GRACE_HOURS)


def _plausibly_just_arrived(ep: SonarrEpisode) -> bool:
    return arrival_is_plausible(ep.air_date_utc)


def _date_moved(before: str | None, after: str | None) -> bool:
    """Whether an air date changed in a way worth telling someone about.

    Only a change of calendar day counts, and only into the future. Sonarr
    nudges times by minutes routinely, and an episode that has already aired
    moving on paper is bookkeeping rather than news.
    """
    if not before or not after or before == after:
        return False
    try:
        was = datetime.fromisoformat(before)
        now_at = datetime.fromisoformat(after)
    except ValueError:
        return False
    was = was if was.tzinfo else was.replace(tzinfo=UTC)
    now_at = now_at if now_at.tzinfo else now_at.replace(tzinfo=UTC)
    if was.date() == now_at.date():
        return False
    return now_at > datetime.now(UTC)


def upsert_episode(conn: sqlite3.Connection, series_id: int, ep: SonarrEpisode) -> int:
    """Insert or update one episode.

    arrived_at means "we watched this become available", not "we first saw it
    with a file". Those are the same thing for an episode we have been
    tracking and wildly different for one we have not: pulling a six-season
    back catalogue would otherwise stamp every episode as arriving today.

    So it is set on an observed transition from no-file to file, or on a first
    sighting only when the air date makes that believable. Once set it never
    moves, so a quality upgrade cannot look like a fresh arrival.
    """
    existing = conn.execute(
        "SELECT id, has_file, arrived_at, air_date_utc FROM episodes "
        "WHERE series_id = ? AND season = ? AND episode = ?",
        (series_id, ep.season, ep.episode),
    ).fetchone()

    now = utcnow()
    if existing:
        became_available = bool(ep.has_file) and not existing["has_file"]
        arrived_at = existing["arrived_at"] or (now if became_available else None)
    else:
        arrived_at = now if ep.has_file and _plausibly_just_arrived(ep) else None

    if existing:
        if _date_moved(existing["air_date_utc"], ep.air_date_utc):
            conn.execute(
                "INSERT INTO schedule_changes (episode_id, old_date, new_date, detected_at) "
                "VALUES (?, ?, ?, ?)",
                (existing["id"], existing["air_date_utc"], ep.air_date_utc, now),
            )
        conn.execute(
            """
            UPDATE episodes SET
                sonarr_episode_id = COALESCE(?, sonarr_episode_id),
                title = ?, air_date_utc = ?, runtime = COALESCE(?, runtime),
                monitored = ?, has_file = ?, arrived_at = ?, finale_type = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                ep.sonarr_episode_id, ep.title, ep.air_date_utc, ep.runtime,
                int(ep.monitored), int(ep.has_file), arrived_at, ep.finale_type,
                now, existing["id"],
            ),
        )
        return int(existing["id"])

    cur = conn.execute(
        """
        INSERT INTO episodes (
            series_id, sonarr_episode_id, season, episode, title,
            air_date_utc, runtime, monitored, has_file, arrived_at,
            finale_type, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            series_id, ep.sonarr_episode_id, ep.season, ep.episode, ep.title,
            ep.air_date_utc, ep.runtime, int(ep.monitored), int(ep.has_file),
            arrived_at, ep.finale_type, now,
        ),
    )
    return int(cur.lastrowid or 0)


def set_outlook(
    conn: sqlite3.Connection,
    series_id: int,
    outlook: str,
    *,
    tmdb_status: str | None = None,
    in_production: bool | None = None,
    tmdb_id: int | None = None,
    latest_aired_season: int | None = None,
) -> None:
    fields: dict[str, Any] = {"outlook": outlook, "outlook_computed_at": utcnow()}
    if tmdb_status is not None:
        fields["tmdb_status"] = tmdb_status
    if in_production is not None:
        fields["in_production"] = int(in_production)
    if tmdb_id is not None:
        fields["tmdb_id"] = tmdb_id
    if latest_aired_season is not None:
        fields["latest_aired_season"] = latest_aired_season
    _apply(conn, series_id, fields, overwrite=True)


def set_last_watched(conn: sqlite3.Connection, plex_rating_key: str, watched_at: str) -> None:
    conn.execute(
        "UPDATE series SET last_watched_at = ?, updated_at = ? WHERE plex_rating_key = ?",
        (watched_at, utcnow(), plex_rating_key),
    )


# ── Pinning ──────────────────────────────────────


def refresh_pinned_flag(conn: sqlite3.Connection, series_id: int) -> None:
    """Keep series.pinned in step with the pins table.

    It is denormalised, and means "pinned by at least one user". The sync
    jobs want precisely that question -- the calendar and availability
    fetches should cover anything anyone follows -- so they read this rather
    than joining pins, and needed no changes.
    """
    conn.execute(
        "UPDATE series SET pinned = (SELECT EXISTS (SELECT 1 FROM pins WHERE series_id = ?)), "
        "updated_at = ? WHERE id = ?",
        (series_id, utcnow(), series_id),
    )


def is_pinned_by(conn: sqlite3.Connection, user_id: int, series_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM pins WHERE user_id = ? AND series_id = ?", (user_id, series_id)
        ).fetchone()
        is not None
    )


def set_pinned(
    conn: sqlite3.Connection,
    user_id: int,
    series_id: int,
    pinned: bool,
    *,
    batch: str | None = None,
) -> None:
    if pinned:
        conn.execute(
            "INSERT INTO pins (user_id, series_id, pinned_at, pin_batch) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id, series_id) DO NOTHING",
            (user_id, series_id, utcnow(), batch),
        )
    else:
        conn.execute(
            "DELETE FROM pins WHERE user_id = ? AND series_id = ?", (user_id, series_id)
        )
    refresh_pinned_flag(conn, series_id)


def set_notify(conn: sqlite3.Connection, user_id: int, series_id: int, notify: bool) -> None:
    """Per-series notification opt-out, per user."""
    conn.execute(
        "UPDATE pins SET notify = ? WHERE user_id = ? AND series_id = ?",
        (int(notify), user_id, series_id),
    )


def bulk_pin(conn: sqlite3.Connection, user_id: int, series_ids: list[int]) -> tuple[int, str]:
    """Pin many series under one batch id, so the action can be undone as a unit."""
    batch = uuid.uuid4().hex[:12]
    added = 0
    for sid in series_ids:
        if is_pinned_by(conn, user_id, sid):
            continue
        set_pinned(conn, user_id, sid, True, batch=batch)
        added += 1
    return added, batch


def undo_bulk_pin(conn: sqlite3.Connection, user_id: int, batch: str) -> int:
    affected = [
        int(r["series_id"])
        for r in conn.execute(
            "SELECT series_id FROM pins WHERE user_id = ? AND pin_batch = ?", (user_id, batch)
        )
    ]
    conn.execute("DELETE FROM pins WHERE user_id = ? AND pin_batch = ?", (user_id, batch))
    for sid in affected:
        refresh_pinned_flag(conn, sid)
    return len(affected)


def latest_bulk_batch(conn: sqlite3.Connection, user_id: int) -> str | None:
    row = conn.execute(
        "SELECT pin_batch FROM pins WHERE user_id = ? AND pin_batch IS NOT NULL "
        "ORDER BY pinned_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    return row["pin_batch"] if row else None


def adopt_orphaned_pins(conn: sqlite3.Connection, user_id: int) -> int:
    """Hand pins made before there were accounts to the first admin.

    Without this, upgrading silently empties the pin list and the only clue
    is a calendar that has gone blank.
    """
    rows = conn.execute(
        "SELECT id FROM series WHERE pinned = 1 "
        "AND NOT EXISTS (SELECT 1 FROM pins WHERE pins.series_id = series.id)"
    ).fetchall()
    for row in rows:
        set_pinned(conn, user_id, int(row["id"]), True)
    return len(rows)


# ── Library browsing (SPEC §11) ──────────────────

#: A 2000-series library is the normal case, not the extreme one, so the grid
#: pages. Facets narrow; pagination makes the un-narrowed view survivable.
PAGE_SIZE = 60

SORTS: dict[str, str] = {
    # NULLs sort smallest in SQLite, so DESC already puts never-watched last.
    "recent": "s.last_watched_at DESC, s.sort_title ASC",
    "title": "s.sort_title ASC",
    # ...but ASC would put undated first, which is the opposite of useful.
    "next": "s.next_airing IS NULL, s.next_airing ASC",
    "outlook": "outlook_rank ASC, s.sort_title ASC",
}

#: Ladder order from SPEC §10, so "sort by outlook" reads as most-imminent
#: first rather than alphabetically by a label nobody thinks in.
OUTLOOK_RANK = [
    "dated", "announced", "in_production", "between_seasons",
    "dormant", "cancelled", "ended", "unknown",
]

PIN_STATES = ("all", "pinned", "unpinned")


@dataclass(frozen=True)
class LibraryFilter:
    """Everything the library view can be narrowed by. Values within a facet
    are OR'd, facets are AND'd together (SPEC §11)."""

    search: str = ""
    sections: tuple[int, ...] = ()
    statuses: tuple[str, ...] = ()
    outlooks: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    pinned: str = "all"
    sort: str = "recent"
    page: int = 1
    #: Whose pins "pinned"/"unpinned" refers to. Not a URL parameter -- it
    #: comes from the session, so nobody can filter by another user's list.
    user_id: int = 0

    def without(self, facet: str) -> LibraryFilter:
        """This filter with one facet cleared.

        Facet counts are computed against the *other* facets, so a count
        answers "what would I get if I ticked this too" rather than
        "how many of my current results have this value", which is always
        either the whole result set or zero.
        """
        return replace(self, **{facet: ()})


def _clauses(f: LibraryFilter) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    params: list[Any] = []

    if f.search:
        where.append("(s.title LIKE ? OR s.sort_title LIKE ?)")
        like = f"%{f.search}%"
        params += [like, like]

    for column, values in (
        ("s.plex_section_id", f.sections),
        ("s.sonarr_status", f.statuses),
        ("s.outlook", f.outlooks),
        ("s.network", f.networks),
    ):
        if values:
            where.append(f"{column} IN ({','.join('?' * len(values))})")
            params += list(values)

    if f.genres:
        where.append(
            "EXISTS (SELECT 1 FROM series_genres sg JOIN genres g ON g.id = sg.genre_id "
            f"WHERE sg.series_id = s.id AND g.name IN ({','.join('?' * len(f.genres))}))"
        )
        params += list(f.genres)

    mine = "EXISTS (SELECT 1 FROM pins p WHERE p.series_id = s.id AND p.user_id = ?)"
    if f.pinned == "pinned":
        where.append(mine)
        params.append(f.user_id)
    elif f.pinned == "unpinned":
        where.append("NOT " + mine)
        params.append(f.user_id)

    return where, params


def _where_sql(f: LibraryFilter) -> tuple[str, list[Any]]:
    where, params = _clauses(f)
    return (" WHERE " + " AND ".join(where)) if where else "", params


def _rank_case() -> str:
    arms = " ".join(
        f"WHEN {value!r} THEN {i}" for i, value in enumerate(OUTLOOK_RANK)
    )
    return f"CASE s.outlook {arms} ELSE {len(OUTLOOK_RANK)} END"


def count_series(conn: sqlite3.Connection, f: LibraryFilter) -> int:
    sql, params = _where_sql(f)
    return int(conn.execute(f"SELECT count(*) AS n FROM series s{sql}", params).fetchone()["n"])


def query_series(conn: sqlite3.Connection, f: LibraryFilter) -> list[sqlite3.Row]:
    sql, params = _where_sql(f)
    order = SORTS.get(f.sort, SORTS["recent"])
    page = max(1, f.page)
    # is_pinned, not pinned: s.* already carries the any-user flag, and an
    # unaliased column would be silently shadowed by it.
    return list(
        conn.execute(
            f"SELECT s.*, {_rank_case()} AS outlook_rank, "
            "EXISTS (SELECT 1 FROM pins p WHERE p.series_id = s.id AND p.user_id = ?) AS is_pinned "
            f"FROM series s{sql} ORDER BY {order} LIMIT ? OFFSET ?",
            [f.user_id, *params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
    )


def matching_ids(conn: sqlite3.Connection, f: LibraryFilter) -> list[int]:
    """Every id the filter matches, ignoring pagination.

    Bulk pin re-runs the filter server-side rather than trusting a list of
    ids from the browser, so there is no stale-id class of bug (SPEC §11).
    """
    sql, params = _where_sql(f)
    return [int(r["id"]) for r in conn.execute(f"SELECT s.id FROM series s{sql}", params)]


def _facet(
    conn: sqlite3.Connection, f: LibraryFilter, facet: str, select: str, join: str = ""
) -> list[dict[str, Any]]:
    sql, params = _where_sql(f.without(facet))
    rows = conn.execute(
        f"SELECT {select} AS value, count(DISTINCT s.id) AS n FROM series s{join}{sql} "
        "GROUP BY value HAVING value IS NOT NULL AND value != '' ORDER BY n DESC, value ASC",
        params,
    )
    return [{"value": r["value"], "count": r["n"]} for r in rows]


def facet_counts(conn: sqlite3.Connection, f: LibraryFilter) -> dict[str, list[dict[str, Any]]]:
    """Counts per facet value, for the rail. One GROUP BY each."""
    genre_join = (
        " JOIN series_genres sg ON sg.series_id = s.id JOIN genres g ON g.id = sg.genre_id"
    )
    return {
        "sections": _facet(conn, f, "sections", "s.plex_section_id"),
        "statuses": _facet(conn, f, "statuses", "s.sonarr_status"),
        "outlooks": _facet(conn, f, "outlooks", "s.outlook"),
        "genres": _facet(conn, f, "genres", "g.name", genre_join),
        "networks": _facet(conn, f, "networks", "s.network"),
    }


def pinned_count(conn: sqlite3.Connection, user_id: int) -> int:
    return int(
        conn.execute(
            "SELECT count(*) AS n FROM pins WHERE user_id = ?", (user_id,)
        ).fetchone()["n"]
    )


def get_series(
    conn: sqlite3.Connection, series_id: int, user_id: int = 0
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT s.*, {_rank_case()} AS outlook_rank, "
        "EXISTS (SELECT 1 FROM pins p WHERE p.series_id = s.id AND p.user_id = ?) AS is_pinned "
        "FROM series s WHERE s.id = ?",
        (user_id, series_id),
    ).fetchone()


# ── Calendar (SPEC §13) ──────────────────────────

_EPISODE_SELECT = """
    -- Aliased: e.* already carries a `title` (the episode's), and an
    -- unaliased s.title is silently shadowed by it.
    SELECT e.*, s.title AS series_title, s.id AS series_id, s.outlook, s.poster_url,
           q.percent AS dl_percent, q.status AS dl_status, q.time_left AS dl_time_left
    FROM episodes e
    JOIN series s ON s.id = e.series_id
    JOIN pins p ON p.series_id = s.id AND p.user_id = ?
    LEFT JOIN download_queue q ON q.sonarr_episode_id = e.sonarr_episode_id
"""


def pinned_episodes(
    conn: sqlite3.Connection,
    user_id: int,
    start: str,
    end: str,
    *,
    include_unmonitored: bool = True,
    include_specials: bool = True,
) -> list[sqlite3.Row]:
    """Pinned episodes airing in a window, chronological.

    The calendar job pulls the window for *all* series and this filters to
    pinned at render time (SPEC §8), so pinning takes effect immediately
    rather than waiting for a refetch.
    """
    monitored = "" if include_unmonitored else " AND e.monitored = 1"
    monitored += "" if include_specials else " AND e.season > 0"
    return list(
        conn.execute(
            _EPISODE_SELECT
            + " WHERE e.air_date_utc >= ? AND e.air_date_utc < ?"
            + monitored
            + " ORDER BY e.air_date_utc ASC, s.sort_title ASC",
            (user_id, start, end),
        )
    )


def overdue_episodes(
    conn: sqlite3.Connection,
    user_id: int,
    since: str,
    now: str,
    *,
    include_specials: bool = True,
) -> list[sqlite3.Row]:
    """Aired, not in Plex, not too long ago to still care about.

    Bounded by `since` because an unbounded query resurfaces every gap in the
    back catalogue, which buries the two episodes that actually went missing
    this week.

    Unmonitored episodes are excluded unconditionally, whatever the display
    setting says. "Aired and never arrived" is only a complaint about
    something you asked Sonarr to fetch.
    """
    return list(
        conn.execute(
            _EPISODE_SELECT
            + " WHERE e.air_date_utc >= ? AND e.air_date_utc < ?"
            + " AND e.has_file = 0 AND e.in_plex = 0 AND e.monitored = 1"
            + ("" if include_specials else " AND e.season > 0")
            + " ORDER BY e.air_date_utc DESC",
            (user_id, since, now),
        )
    )


def pinned_by_outlook(
    conn: sqlite3.Connection, user_id: int, outlooks: tuple[str, ...]
) -> list[sqlite3.Row]:
    """Pinned series in given outlook states, for the calendar's tail sections."""
    marks = ",".join("?" * len(outlooks))
    return list(
        conn.execute(
            "SELECT s.* FROM series s JOIN pins p ON p.series_id = s.id AND p.user_id = ? "
            f"WHERE s.outlook IN ({marks}) ORDER BY s.sort_title ASC",
            (user_id, *outlooks),
        )
    )


# ── Series detail ────────────────────────────────


def series_episodes(conn: sqlite3.Connection, series_id: int) -> list[sqlite3.Row]:
    """Every episode we hold for one series, newest season first.

    Only covers the calendar window the sync pulls (SPEC §8), so this is
    "what's near", not a full episode guide.
    """
    return list(
        conn.execute(
            "SELECT * FROM episodes WHERE series_id = ? "
            "ORDER BY season DESC, episode ASC",
            (series_id,),
        )
    )


def genres_for(conn: sqlite3.Connection, series_id: int) -> list[str]:
    return [
        r["name"]
        for r in conn.execute(
            "SELECT g.name FROM genres g JOIN series_genres sg ON sg.genre_id = g.id "
            "WHERE sg.series_id = ? ORDER BY g.name",
            (series_id,),
        )
    ]


def record_sections(conn: sqlite3.Connection, sections: list[dict[str, Any]]) -> None:
    """Remember the Plex library names, so facets can use them."""
    for sec in sections:
        conn.execute(
            "INSERT INTO plex_sections (id, title, type, agent, seen_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title = excluded.title, type = excluded.type, "
            "agent = excluded.agent, seen_at = excluded.seen_at",
            (sec["id"], sec["title"], sec.get("type"), sec.get("agent"), utcnow()),
        )


def section_titles(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        int(r["id"]): r["title"] for r in conn.execute("SELECT id, title FROM plex_sections")
    }


# ── Discover ─────────────────────────────────────

#: 2000 series means an unbounded discover page is a scrolling wall. These
#: are generous enough that the cap is never the thing you notice.
DISCOVER_LIMIT = 60


def _unpinned(user_id: int) -> str:
    return "NOT EXISTS (SELECT 1 FROM pins p WHERE p.series_id = s.id AND p.user_id = ?)"


def discover_dated(
    conn: sqlite3.Connection, user_id: int, *, now: str, until: str | None = None
) -> list[sqlite3.Row]:
    """Series with a dated episode coming that this user has not pinned.

    The whole point of the page: a 2000-series library is a list of things
    you have forgotten about, and a date is the strongest signal that one of
    them is worth remembering.
    """
    clause = "s.next_airing > ?"
    params: list[Any] = [user_id, now]
    if until:
        clause += " AND s.next_airing < ?"
        params.append(until)
    return list(
        conn.execute(
            f"SELECT s.* FROM series s WHERE {_unpinned(user_id)} "
            f"AND s.next_airing IS NOT NULL AND {clause} "
            "ORDER BY s.next_airing ASC LIMIT ?",
            [*params, DISCOVER_LIMIT],
        )
    )


def discover_announced(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """Unpinned series with a season announced but no dates yet."""
    return list(
        conn.execute(
            f"SELECT s.* FROM series s WHERE {_unpinned(user_id)} "
            "AND s.outlook IN ('announced', 'in_production') AND s.next_airing IS NULL "
            "ORDER BY s.last_watched_at DESC, s.sort_title ASC LIMIT ?",
            (user_id, DISCOVER_LIMIT),
        )
    )


def discover_counts(conn: sqlite3.Connection, user_id: int, now: str) -> dict[str, int]:
    dated = conn.execute(
        f"SELECT count(*) AS n FROM series s WHERE {_unpinned(user_id)} "
        "AND s.next_airing IS NOT NULL AND s.next_airing > ?",
        (user_id, now),
    ).fetchone()["n"]
    announced = conn.execute(
        f"SELECT count(*) AS n FROM series s WHERE {_unpinned(user_id)} "
        "AND s.outlook IN ('announced', 'in_production') AND s.next_airing IS NULL",
        (user_id,),
    ).fetchone()["n"]
    return {"dated": int(dated), "announced": int(announced)}


# ── Full episode guide ───────────────────────────


def mark_episodes_synced(conn: sqlite3.Connection, series_id: int) -> None:
    conn.execute(
        "UPDATE series SET episodes_synced_at = ?, updated_at = ? WHERE id = ?",
        (utcnow(), utcnow(), series_id),
    )


def episodes_by_season(
    conn: sqlite3.Connection, series_id: int
) -> list[tuple[int, list[sqlite3.Row]]]:
    """Every episode we hold, grouped by season, in broadcast order.

    Seasons ascend because episodes within them do, and mixing the two reads
    as a bug. Which season is *interesting* is a separate question, answered
    by opening the latest one rather than by reversing the list.

    Specials are season 0 and sort last rather than first: they are a
    footnote to a series, not the beginning of it.
    """
    rows = list(
        conn.execute(
            "SELECT * FROM episodes WHERE series_id = ? ORDER BY season ASC, episode ASC",
            (series_id,),
        )
    )
    seasons: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        seasons.setdefault(int(row["season"]), []).append(row)
    return sorted(seasons.items(), key=lambda kv: (kv[0] == 0, kv[0]))


def latest_season(seasons: list[tuple[int, Any]]) -> int | None:
    """The season worth expanding: the newest real one, specials aside."""
    numbers = [n for n, _ in seasons if n != 0]
    if numbers:
        return max(numbers)
    return seasons[0][0] if seasons else None


# ── Ready to watch ───────────────────────────────

#: How far back "recently arrived" reaches. Long enough to cover a fortnight
#: away, short enough that it stays a shortlist rather than an inventory.
READY_DAYS = 21


def ready_to_watch(
    conn: sqlite3.Connection,
    user_id: int,
    since: str,
    until: str,
    *,
    max_runtime: int | None = None,
) -> list[tuple[sqlite3.Row, list[sqlite3.Row]]]:
    """Pinned episodes that have arrived recently, grouped by series.

    The calendar answers "what is coming"; this answers "what can I put on
    tonight", which for anyone who watches after downloading is the question
    they actually have.

    Grouped by series because that is the unit of a viewing session — four
    episodes of one show is one decision, not four.

    Anything already watched is gone: a list that never shrinks is an
    inventory, not a shortlist.

    Recency falls back to the air date when we never watched the file appear.
    arrived_at only exists for episodes Pinnarr saw arrive, so insisting on it
    would hide everything already in Plex before it was installed — which is
    most of a library, and exactly the thing you might want to watch.
    """
    rows = list(
        conn.execute(
            """
            SELECT e.*, s.id AS series_id, s.title AS series_title, s.poster_url,
                   s.remote_poster, s.outlook,
                   COALESCE(e.arrived_at, e.air_date_utc) AS recency
            FROM episodes e
            JOIN series s ON s.id = e.series_id
            JOIN pins p ON p.series_id = s.id AND p.user_id = ?
            WHERE (e.has_file = 1 OR e.in_plex = 1)
              AND COALESCE(e.arrived_at, e.air_date_utc) >= ?
              AND COALESCE(e.arrived_at, e.air_date_utc) <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM episode_watches w
                  WHERE w.episode_id = e.id AND w.user_id = ?
              )
              AND (? IS NULL OR (e.runtime IS NOT NULL AND e.runtime <= ?))
            ORDER BY recency DESC, e.season ASC, e.episode ASC
            """,
            (user_id, since, until, user_id, max_runtime, max_runtime),
        )
    )

    grouped: dict[int, list[sqlite3.Row]] = {}
    order: list[int] = []
    for row in rows:
        sid = int(row["series_id"])
        if sid not in grouped:
            grouped[sid] = []
            order.append(sid)
        grouped[sid].append(row)

    series_rows = {}
    if order:
        marks = ",".join("?" * len(order))
        series_rows = {
            int(r["id"]): r
            for r in conn.execute(f"SELECT * FROM series WHERE id IN ({marks})", order)
        }
    # `order` preserves newest-arrival-first, which the IN query does not.
    return [(series_rows[sid], grouped[sid]) for sid in order if sid in series_rows]


# ── Pin hygiene ──────────────────────────────────


def finished_pins(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """Pinned series that are over and have nothing left to air.

    §10 makes this argument for `dormant`; it applies at least as strongly to
    shows that genuinely ended. A pin that can never produce another episode
    is not a subscription, it is a souvenir.
    """
    return list(
        conn.execute(
            """
            SELECT s.* FROM series s
            JOIN pins p ON p.series_id = s.id AND p.user_id = ?
            WHERE s.outlook IN ('ended', 'cancelled')
              AND (s.next_airing IS NULL OR s.next_airing < ?)
            ORDER BY s.sort_title
            """,
            (user_id, utcnow()),
        )
    )


def retire(conn: sqlite3.Connection, user_id: int, series_ids: list[int]) -> tuple[int, str]:
    """Unpin many at once, as one undoable batch."""
    batch = uuid.uuid4().hex[:12]
    removed = 0
    for series_id in series_ids:
        cur = conn.execute(
            "DELETE FROM pins WHERE user_id = ? AND series_id = ?", (user_id, series_id)
        )
        if cur.rowcount:
            refresh_pinned_flag(conn, series_id)
            removed += 1
    return removed, batch



def season_progress(conn: sqlite3.Connection, series_id: int) -> dict[int, dict[str, int]]:
    """Per season: how much exists, has aired, and is actually here.

    Answers "am I waiting or am I behind?", which otherwise needs the episode
    list and some counting.
    """
    rows = conn.execute(
        """
        SELECT season,
               count(*) AS total,
               sum(CASE WHEN air_date_utc IS NOT NULL AND air_date_utc <= ?
                        THEN 1 ELSE 0 END) AS aired,
               sum(CASE WHEN has_file = 1 OR in_plex = 1 THEN 1 ELSE 0 END) AS have,
               sum(CASE WHEN in_plex = 1 THEN 1 ELSE 0 END) AS confirmed
        FROM episodes WHERE series_id = ? GROUP BY season
        """,
        (utcnow(), series_id),
    )
    return {
        int(r["season"]): {
            "total": int(r["total"]),
            "aired": int(r["aired"] or 0),
            "have": int(r["have"] or 0),
            "confirmed": int(r["confirmed"] or 0),
        }
        for r in rows
    }


def set_ratings(conn: sqlite3.Connection, series_id: int, season: int,
                ratings: dict[int, float]) -> int:
    updated = 0
    for episode, score in ratings.items():
        updated += conn.execute(
            "UPDATE episodes SET rating = ? WHERE series_id = ? AND season = ? AND episode = ?",
            (score, series_id, season, episode),
        ).rowcount
    return updated


def plex_shortfall(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """Pinned series where Sonarr holds more episodes than Plex shows.

    Sonarr having a file that Plex has not indexed means a naming problem, a
    failed scan, or a file Plex cannot read. Nothing else notices, and it is
    invisible until you go looking for an episode Sonarr insists is there.

    Only meaningful for series the availability job has actually confirmed:
    an unchecked series has in_plex = 0 everywhere, which says nothing about
    Plex and would report the entire back catalogue as missing.
    """
    return list(
        conn.execute(
            """
            SELECT s.id, s.title, s.poster_url,
                   sum(CASE WHEN e.has_file = 1 THEN 1 ELSE 0 END) AS have_sonarr,
                   sum(CASE WHEN e.in_plex = 1 THEN 1 ELSE 0 END) AS have_plex
            FROM series s
            JOIN pins p ON p.series_id = s.id AND p.user_id = ?
            JOIN episodes e ON e.series_id = s.id
            WHERE s.plex_rating_key IS NOT NULL
              AND s.plex_checked_at IS NOT NULL
              AND e.season > 0
            GROUP BY s.id
            HAVING have_sonarr > have_plex
            ORDER BY (have_sonarr - have_plex) DESC, s.sort_title
            """,
            (user_id,),
        )
    )


def store_recommendations(conn: sqlite3.Connection, series_id: int,
                          items: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM recommendations WHERE source_series_id = ?", (series_id,))
    for item in items:
        conn.execute(
            "INSERT INTO recommendations (source_series_id, tmdb_id, title, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (series_id, item["tmdb_id"], item.get("title"), utcnow()),
        )


def suggested(conn: sqlite3.Connection, user_id: int, limit: int = 30) -> list[sqlite3.Row]:
    """Shows already in the library that resemble what this user pins.

    Discover answers "what is coming". This answers "what have I forgotten I
    own", which at two thousand series is the larger question.
    """
    return list(
        conn.execute(
            """
            SELECT s.*, count(*) AS votes,
                   group_concat(src.title, ', ') AS because
            FROM recommendations r
            JOIN series src ON src.id = r.source_series_id
            JOIN pins mine ON mine.series_id = src.id AND mine.user_id = ?
            JOIN series s ON s.tmdb_id = r.tmdb_id
            WHERE NOT EXISTS (
                SELECT 1 FROM pins p WHERE p.series_id = s.id AND p.user_id = ?
            )
            GROUP BY s.id
            ORDER BY votes DESC, s.last_watched_at DESC, s.sort_title
            LIMIT ?
            """,
            (user_id, user_id, limit),
        )
    )


# ── Gaps ─────────────────────────────────────────


def gaps(conn: sqlite3.Connection, user_id: int) -> list[tuple[sqlite3.Row, list[sqlite3.Row]]]:
    """Aired episodes of pinned shows that never turned up, grouped by series.

    Distinct from "aired, not arrived" on the calendar, which is bounded to
    30 days so this week's problem isn't buried. This is the whole back
    catalogue: a hole in the middle of season two is invisible until you sit
    down to watch it and can't.

    Specials are excluded — a missing Christmas one-off is not a gap in a
    story — and so is anything Sonarr isn't chasing.
    """
    rows = list(
        conn.execute(
            """
            SELECT e.*, s.id AS series_id, s.title AS series_title,
                   s.episodes_synced_at
            FROM episodes e
            JOIN series s ON s.id = e.series_id
            JOIN pins p ON p.series_id = s.id AND p.user_id = ?
            WHERE e.season > 0
              AND e.monitored = 1
              AND e.has_file = 0 AND e.in_plex = 0
              AND e.air_date_utc IS NOT NULL AND e.air_date_utc < ?
            ORDER BY s.sort_title, e.season, e.episode
            """,
            (user_id, utcnow()),
        )
    )

    grouped: dict[int, list[sqlite3.Row]] = {}
    order: list[int] = []
    for row in rows:
        sid = int(row["series_id"])
        if sid not in grouped:
            grouped[sid] = []
            order.append(sid)
        grouped[sid].append(row)

    series_rows = {}
    if order:
        marks = ",".join("?" * len(order))
        series_rows = {
            int(r["id"]): r
            for r in conn.execute(f"SELECT * FROM series WHERE id IN ({marks})", order)
        }
    return [(series_rows[sid], grouped[sid]) for sid in order if sid in series_rows]



def mark_watched(conn: sqlite3.Connection, user_id: int, plex_rating_key: str,
                 season: int, episode: int, watched_at: str) -> bool:
    """Record that one viewer has seen an episode. Earliest play wins.

    Per user, because everything else about a pin list is. Attributing one
    person's viewing to everybody would make "watched" mean less than nothing.
    """
    row = conn.execute(
        "SELECT e.id FROM episodes e JOIN series s ON s.id = e.series_id "
        "WHERE s.plex_rating_key = ? AND e.season = ? AND e.episode = ?",
        (plex_rating_key, season, episode),
    ).fetchone()
    if row is None:
        return False

    conn.execute(
        """
        INSERT INTO episode_watches (user_id, episode_id, watched_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, episode_id) DO UPDATE SET
            watched_at = MIN(episode_watches.watched_at, excluded.watched_at)
        """,
        (user_id, int(row["id"]), watched_at),
    )
    return True


def watch_progress(conn: sqlite3.Connection, user_id: int) -> dict[int, dict[str, int]]:
    """Per series: how many episodes this user holds, and how many they have seen."""
    rows = conn.execute(
        """
        SELECT e.series_id,
               count(*) AS have,
               sum(CASE WHEN w.watched_at IS NOT NULL THEN 1 ELSE 0 END) AS seen
        FROM episodes e
        LEFT JOIN episode_watches w ON w.episode_id = e.id AND w.user_id = ?
        WHERE e.season > 0 AND (e.has_file = 1 OR e.in_plex = 1)
        GROUP BY e.series_id
        """,
        (user_id,),
    )
    return {
        int(r["series_id"]): {"have": int(r["have"]), "seen": int(r["seen"] or 0)}
        for r in rows
    }
