"""Recompute season outlook for every series, enriching with TMDB where useful.

Two-tier deliberately:

- The outlook ladder is recomputed for the *whole* library from local Sonarr
  data. `dated`, `announced`, `hiatus` and `dormant` need no external call,
  so the library filters work library-wide even with no TMDB key at all.
- TMDB is queried only where it adds something Sonarr can't say: cancelled
  vs ended, and in_production. Pinned series first, then continuing series,
  up to a nightly cap.
"""

from __future__ import annotations

import asyncio
import logging

from app.clients.http import UpstreamError
from app.clients.tmdb import TmdbClient, TmdbShow
from app.config import get_settings
from app.db import session
from app.jobs import tracked
from app.outlook import compute_outlook
from app.repo import set_outlook

log = logging.getLogger(__name__)

#: Cap on TMDB lookups per run. A few hundred is well inside their limits;
#: the cap exists so a 3000-show library doesn't turn into a nightly crawl.
TMDB_NIGHTLY_CAP = 400
TMDB_CONCURRENCY = 8


def _latest_aired_season(conn, series_id: int) -> int | None:
    """Highest season number with at least one episode that has already aired."""
    row = conn.execute(
        """
        SELECT MAX(season) AS s FROM episodes
        WHERE series_id = ? AND season > 0
          AND air_date_utc IS NOT NULL
          AND air_date_utc <= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now')
        """,
        (series_id,),
    ).fetchone()
    return row["s"] if row and row["s"] is not None else None


async def _fetch_tmdb(
    client: TmdbClient, semaphore: asyncio.Semaphore, series_id: int,
    tmdb_id: int | None, tvdb_id: int | None,
) -> tuple[int, TmdbShow | None]:
    async with semaphore:
        try:
            if not tmdb_id and tvdb_id:
                tmdb_id = await client.find_by_tvdb(tvdb_id)
            if not tmdb_id:
                return series_id, None
            return series_id, await client.tv_details(tmdb_id)
        except UpstreamError as exc:
            log.warning("TMDB lookup failed for series %s: %s", series_id, exc)
            return series_id, None


async def resolve_missing_tmdb_ids(limit: int = 200) -> int:
    """Fill in TMDB ids for series that arrived without one.

    Sonarr's metadata is TVDB's, and its tmdbId is empty far more often than
    not — so a show Sonarr has just added has no TMDB id at all. Everything
    that talks to Overseerr keys on exactly that: a request you have just
    made cannot be matched to the series it produced until this has run,
    which is why it is worth doing on demand rather than only at 03:30.
    """
    with session() as conn:
        rows = list(
            conn.execute(
                "SELECT id, tvdb_id FROM series "
                "WHERE tmdb_id IS NULL AND tvdb_id IS NOT NULL LIMIT ?",
                (limit,),
            )
        )
    if not rows:
        return 0

    client = TmdbClient()
    found = 0
    for row in rows:
        try:
            tmdb_id = await client.find_by_tvdb(int(row["tvdb_id"]))
        except UpstreamError as exc:
            log.warning("TMDB lookup failed for series %s: %s", row["id"], exc)
            continue
        if not tmdb_id:
            continue
        with session() as conn:
            conn.execute(
                "UPDATE series SET tmdb_id = ? WHERE id = ? AND tmdb_id IS NULL",
                (tmdb_id, row["id"]),
            )
        found += 1
    return found


@tracked("find_requested")
async def find_requested() -> str:
    """Go and look for something just asked for.

    Overseerr tells Sonarr; this asks Sonarr what it now has and then gives
    the new rows a TMDB id, because without one the show cannot be matched
    back to the request that produced it and the card keeps pointing at TMDB
    for ever.
    """
    from app.jobs.sonarr_sync import sync_sonarr_series

    note = await sync_sonarr_series()
    if not get_settings().tmdb_configured:
        return f"{note}; TMDB not configured, so no ids resolved"
    found = await resolve_missing_tmdb_ids()
    return f"{note}; {found} TMDB id(s) resolved"


@tracked("tmdb_status")
async def sync_outlook() -> str:
    settings = get_settings()

    with session() as conn:
        rows = conn.execute(
            """
            SELECT id, tvdb_id, tmdb_id, pinned, sonarr_status, tmdb_status,
                   in_production, next_airing, previous_airing, latest_season
            FROM series
            ORDER BY pinned DESC, (sonarr_status = 'continuing') DESC, id
            """
        ).fetchall()

    if not rows:
        return "no series yet"

    # Before the budget bites. Resolving an id is one cheap call and is what
    # everything joining Pinnarr to Overseerr keys on, so it must not queue
    # behind four hundred outlook lookups on a two thousand series library —
    # an unpinned show sorts last there and could wait days for an id.
    resolved = 0
    if settings.tmdb_configured:
        resolved = await resolve_missing_tmdb_ids()

    # ── Tier 2: TMDB enrichment, budget-limited ──
    enriched: dict[int, TmdbShow] = {}
    if settings.tmdb_configured:
        candidates = [
            r for r in rows
            if r["pinned"] or (r["sonarr_status"] or "").lower() == "continuing"
        ][:TMDB_NIGHTLY_CAP]

        client = TmdbClient()
        semaphore = asyncio.Semaphore(TMDB_CONCURRENCY)
        results = await asyncio.gather(
            *(
                _fetch_tmdb(client, semaphore, r["id"], r["tmdb_id"], r["tvdb_id"])
                for r in candidates
            )
        )
        enriched = {sid: show for sid, show in results if show is not None}
        if len(candidates) == TMDB_NIGHTLY_CAP:
            log.info(
                "TMDB enrichment hit the nightly cap of %d; remaining series keep "
                "their previous status", TMDB_NIGHTLY_CAP,
            )
    else:
        log.info("TMDB not configured — outlook computed from Sonarr data only, "
                 "so cancelled shows will read as dormant rather than cancelled")

    # ── Tier 1: recompute the ladder for everything ──
    counts: dict[str, int] = {}
    with session() as conn:
        for row in rows:
            series_id = row["id"]
            tmdb = enriched.get(series_id)

            tmdb_status = tmdb.status if tmdb else row["tmdb_status"]
            in_production = tmdb.in_production if tmdb else (
                bool(row["in_production"]) if row["in_production"] is not None else None
            )
            latest_aired = _latest_aired_season(conn, series_id)

            # TMDB's number_of_seasons can know about a season Sonarr hasn't
            # picked up yet, so take whichever is higher.
            latest_season = row["latest_season"]
            if tmdb and tmdb.number_of_seasons:
                latest_season = max(latest_season or 0, tmdb.number_of_seasons)

            outlook = compute_outlook(
                next_airing=row["next_airing"],
                previous_airing=row["previous_airing"],
                latest_season=latest_season,
                latest_aired_season=latest_aired,
                sonarr_status=row["sonarr_status"],
                tmdb_status=tmdb_status,
                in_production=in_production,
                hiatus_months=settings.hiatus_months,
                dormant_months=settings.dormant_months,
            )

            set_outlook(
                conn, series_id, outlook,
                tmdb_status=tmdb_status,
                in_production=in_production,
                tmdb_id=tmdb.tmdb_id if tmdb else None,
                latest_aired_season=latest_aired,
            )
            counts[outlook] = counts.get(outlook, 0) + 1

    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    note = f"{len(rows)} series ({len(enriched)} TMDB-enriched): {summary}"
    if resolved:
        note += f"; {resolved} TMDB id(s) resolved"
    return note
