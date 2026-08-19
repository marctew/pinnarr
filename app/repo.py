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


def upsert_episode(conn: sqlite3.Connection, series_id: int, ep: SonarrEpisode) -> int:
    """Insert or update one episode.

    arrived_at is stamped the first time we see has_file flip true, and never
    moved afterwards — a quality upgrade must not look like a fresh arrival.
    """
    existing = conn.execute(
        "SELECT id, has_file, arrived_at FROM episodes WHERE series_id = ? AND season = ? AND episode = ?",
        (series_id, ep.season, ep.episode),
    ).fetchone()

    now = utcnow()
    arrived_at = existing["arrived_at"] if existing else None
    if ep.has_file and not arrived_at:
        arrived_at = now

    if existing:
        conn.execute(
            """
            UPDATE episodes SET
                sonarr_episode_id = COALESCE(?, sonarr_episode_id),
                title = ?, air_date_utc = ?, runtime = COALESCE(?, runtime),
                monitored = ?, has_file = ?, arrived_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                ep.sonarr_episode_id, ep.title, ep.air_date_utc, ep.runtime,
                int(ep.monitored), int(ep.has_file), arrived_at, now, existing["id"],
            ),
        )
        return int(existing["id"])

    cur = conn.execute(
        """
        INSERT INTO episodes (
            series_id, sonarr_episode_id, season, episode, title,
            air_date_utc, runtime, monitored, has_file, arrived_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            series_id, ep.sonarr_episode_id, ep.season, ep.episode, ep.title,
            ep.air_date_utc, ep.runtime, int(ep.monitored), int(ep.has_file),
            arrived_at, now,
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


def set_pinned(
    conn: sqlite3.Connection, series_id: int, pinned: bool, *, batch: str | None = None
) -> None:
    conn.execute(
        "UPDATE series SET pinned = ?, pinned_at = ?, pin_batch = ?, updated_at = ? WHERE id = ?",
        (int(pinned), utcnow() if pinned else None, batch if pinned else None, utcnow(), series_id),
    )


def bulk_pin(conn: sqlite3.Connection, series_ids: list[int]) -> tuple[int, str]:
    """Pin many series under one batch id, so the action can be undone as a unit."""
    batch = uuid.uuid4().hex[:12]
    unpinned = [
        sid
        for sid in series_ids
        if not (conn.execute("SELECT pinned FROM series WHERE id = ?", (sid,)).fetchone() or {"pinned": 0})["pinned"]
    ]
    for sid in unpinned:
        set_pinned(conn, sid, True, batch=batch)
    return len(unpinned), batch


def undo_bulk_pin(conn: sqlite3.Connection, batch: str) -> int:
    cur = conn.execute(
        "UPDATE series SET pinned = 0, pinned_at = NULL, pin_batch = NULL, updated_at = ? "
        "WHERE pin_batch = ?",
        (utcnow(), batch),
    )
    return cur.rowcount


def latest_bulk_batch(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT pin_batch FROM series WHERE pin_batch IS NOT NULL "
        "ORDER BY pinned_at DESC LIMIT 1"
    ).fetchone()
    return row["pin_batch"] if row else None


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

    if f.pinned == "pinned":
        where.append("s.pinned = 1")
    elif f.pinned == "unpinned":
        where.append("s.pinned = 0")

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
    return list(
        conn.execute(
            f"SELECT s.*, {_rank_case()} AS outlook_rank FROM series s{sql} "
            f"ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
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


def pinned_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) AS n FROM series WHERE pinned = 1").fetchone()["n"])


def get_series(conn: sqlite3.Connection, series_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT s.*, {_rank_case()} AS outlook_rank FROM series s WHERE s.id = ?", (series_id,)
    ).fetchone()


# ── Calendar (SPEC §13) ──────────────────────────

_EPISODE_SELECT = """
    -- Aliased: e.* already carries a `title` (the episode's), and an
    -- unaliased s.title is silently shadowed by it.
    SELECT e.*, s.title AS series_title, s.id AS series_id, s.outlook, s.poster_url
    FROM episodes e JOIN series s ON s.id = e.series_id
    WHERE s.pinned = 1
"""


def pinned_episodes(
    conn: sqlite3.Connection, start: str, end: str
) -> list[sqlite3.Row]:
    """Pinned episodes airing in a window, chronological.

    The calendar job pulls the window for *all* series and this filters to
    pinned at render time (SPEC §8), so pinning takes effect immediately
    rather than waiting for a refetch.
    """
    return list(
        conn.execute(
            _EPISODE_SELECT
            + " AND e.air_date_utc >= ? AND e.air_date_utc < ?"
            + " ORDER BY e.air_date_utc ASC, s.sort_title ASC",
            (start, end),
        )
    )


def overdue_episodes(conn: sqlite3.Connection, since: str, now: str) -> list[sqlite3.Row]:
    """Aired, not in Plex, not too long ago to still care about.

    Bounded by `since` because an unbounded query resurfaces every gap in the
    back catalogue, which buries the two episodes that actually went missing
    this week.
    """
    return list(
        conn.execute(
            _EPISODE_SELECT
            + " AND e.air_date_utc >= ? AND e.air_date_utc < ?"
            + " AND e.has_file = 0 AND e.in_plex = 0 AND e.monitored = 1"
            + " ORDER BY e.air_date_utc DESC",
            (since, now),
        )
    )


def pinned_by_outlook(conn: sqlite3.Connection, outlooks: tuple[str, ...]) -> list[sqlite3.Row]:
    """Pinned series in given outlook states, for the calendar's tail sections."""
    marks = ",".join("?" * len(outlooks))
    return list(
        conn.execute(
            f"SELECT * FROM series WHERE pinned = 1 AND outlook IN ({marks}) "
            "ORDER BY sort_title ASC",
            outlooks,
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
