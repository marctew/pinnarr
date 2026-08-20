"""Why an episode hasn't arrived.

The red "aired, not arrived" row is the state Pinnarr surfaces better than
anything else, and until now it was an accusation with no evidence. Sonarr's
history knows whether nothing was ever found, whether a release was grabbed
and failed, or whether it imported fine and the gap is Plex's.
"""

from __future__ import annotations

import logging
from typing import Any

from app.clients.http import UpstreamError
from app.clients.sonarr import SonarrClient
from app.config import get_settings
from app.db import session

log = logging.getLogger(__name__)

GRABBED = "grabbed"
FAILED = "downloadFailed"
IMPORTED = "downloadFolderImported"


def _summarise(events: list[dict[str, Any]], queued: Any) -> str:
    if queued is not None:
        percent = round(float(queued["percent"] or 0))
        left = queued["time_left"]
        tail = f", {left} left" if left else ""
        return f"Downloading now — {percent}%{tail}."

    kinds = [str(event.get("eventType") or "") for event in events]

    if IMPORTED in kinds:
        return (
            "Sonarr imported this already. If Plex hasn't got it, the library "
            "path or a Plex scan is what to check — not Sonarr."
        )
    if FAILED in kinds:
        return (
            "A download was grabbed and then failed. Sonarr usually retries on "
            "its own; searching again forces it."
        )
    if GRABBED in kinds:
        return "Grabbed, but it hasn't finished importing. Check your download client."
    if not events:
        return (
            "Sonarr has no history for this episode at all — nothing has ever "
            "been found. Either no release exists yet, or none matches your "
            "quality profile."
        )
    return f"Sonarr's most recent event was {kinds[0] or 'unknown'}."


async def why_missing(episode_id: int) -> dict[str, Any]:
    """Explain one episode. Never raises — an unexplained row is still a row."""
    with session() as conn:
        row = conn.execute(
            "SELECT e.sonarr_episode_id, e.season, e.episode, e.has_file, "
            "s.title AS series_title FROM episodes e JOIN series s ON s.id = e.series_id "
            "WHERE e.id = ?",
            (episode_id,),
        ).fetchone()
        queued = None
        if row and row["sonarr_episode_id"]:
            queued = conn.execute(
                "SELECT percent, time_left, status FROM download_queue "
                "WHERE sonarr_episode_id = ?",
                (row["sonarr_episode_id"],),
            ).fetchone()

    if row is None:
        return {"ok": False, "detail": "No such episode."}
    if not row["sonarr_episode_id"]:
        return {"ok": False, "detail": "Sonarr doesn't track this episode."}
    if not get_settings().sonarr_configured:
        return {"ok": False, "detail": "Sonarr isn't configured."}

    try:
        events = await SonarrClient().history_for_episode(int(row["sonarr_episode_id"]))
    except UpstreamError as exc:
        return {"ok": False, "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 — a diagnosis must not 500 a page
        log.exception("history lookup failed")
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    return {"ok": True, "detail": _summarise(events, queued), "events": len(events)}
