"""Nightly tidying.

None of this matters this month. All of it matters on a box you have stopped
thinking about, which is what a rollout eventually turns every install into:
sync_log grows by roughly forty rows a day forever, expired sessions are
never collected, and the poster cache keeps art for series that no longer
exist.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from app.auth import purge_expired
from app.db import session
from app.jobs import tracked
from app.media import cache_dir

log = logging.getLogger(__name__)

#: Runs to keep per job. Enough to see a pattern in a failure, not enough to
#: accumulate meaningfully.
KEEP_RUNS = 100

#: Cached posters untouched for this long are dropped. They cost one request
#: to rebuild and only for a series someone actually looks at again.
POSTER_MAX_AGE_DAYS = 90


def prune_sync_log() -> int:
    with session() as conn:
        return conn.execute(
            """
            DELETE FROM sync_log WHERE id IN (
                SELECT id FROM (
                    SELECT id, row_number() OVER (
                        PARTITION BY job ORDER BY id DESC
                    ) AS rn FROM sync_log
                ) WHERE rn > ?
            )
            """,
            (KEEP_RUNS,),
        ).rowcount


def prune_posters() -> int:
    """Drop artwork for series that no longer exist, and anything stale.

    Files are named `{series_id}-{hash}`, so an orphan is identifiable
    without keeping a second index of what is cached.
    """
    directory = cache_dir()
    with session() as conn:
        live = {int(r["id"]) for r in conn.execute("SELECT id FROM series")}

    cutoff = time.time() - POSTER_MAX_AGE_DAYS * 86400
    removed = 0
    for path in directory.iterdir():
        if not path.is_file():
            continue
        series_id = _series_id_of(path)
        stale = path.stat().st_mtime < cutoff
        orphan = series_id is not None and series_id not in live
        partial = path.suffix == ".part"
        if stale or orphan or partial:
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                log.warning("could not remove cached poster %s: %s", path.name, exc)
    return removed


def _series_id_of(path: Path) -> int | None:
    head = path.name.split("-", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def compact() -> None:
    """VACUUM reclaims what the deletes above freed.

    Outside the usual session() helper: VACUUM cannot run inside a
    transaction, and the helper opens one.
    """
    from app.db import connect

    conn = connect()
    try:
        conn.isolation_level = None
        conn.execute("VACUUM")
    finally:
        conn.close()


@tracked("housekeeping")
async def housekeeping() -> str:
    with session() as conn:
        sessions = purge_expired(conn)
    runs = prune_sync_log()
    posters = prune_posters()
    compact()
    return (
        f"{sessions} expired session(s), {runs} old job run(s), "
        f"{posters} cached poster(s) removed"
    )
