"""Tautulli client — owns watch history and arrival confirmation.

Not strictly required (pinning is manual), but sorting the library by
"recently watched" is what makes a 300-show library pinnable in two
minutes instead of ten.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.clients.http import UpstreamError, request_json
from app.config import get_settings

log = logging.getLogger(__name__)


@dataclass
class WatchRecord:
    grandparent_rating_key: str | None
    title: str | None
    watched_at: str | None


class TautulliClient:
    service = "tautulli"

    def __init__(self) -> None:
        s = get_settings()
        self.base = s.tautulli_url
        self.api_key = s.tautulli_api_key

    async def _cmd(self, cmd: str, **params: Any) -> Any:
        if not self.base or not self.api_key:
            raise UpstreamError(self.service, "not configured")
        payload = await request_json(
            self.service,
            "GET",
            f"{self.base}/api/v2",
            params={"apikey": self.api_key, "cmd": cmd, **params},
        )
        response = (payload or {}).get("response") or {}
        if response.get("result") != "success":
            raise UpstreamError(self.service, f"{cmd}: {response.get('message') or 'failed'}")
        return response.get("data")

    async def ping(self) -> str:
        data = await self._cmd("get_server_info")
        return str((data or {}).get("version", "unknown"))

    async def libraries(self) -> list[dict[str, Any]]:
        return list(await self._cmd("get_libraries") or [])

    async def show_history(self, length: int = 1000) -> list[WatchRecord]:
        """Recent episode plays, newest first.

        `media_type=episode` so we get per-episode rows; the show is
        identified by grandparent_rating_key, which is the Plex ratingKey of
        the series and therefore joins straight onto our series table.
        """
        data = await self._cmd(
            "get_history", media_type="episode", length=length, order_dir="desc"
        )
        rows = (data or {}).get("data") or []
        records = []
        for row in rows:
            stopped = row.get("stopped") or row.get("started")
            watched_at = None
            if stopped:
                try:
                    watched_at = (
                        datetime.fromtimestamp(int(stopped), tz=UTC)
                        .replace(microsecond=0)
                        .isoformat()
                    )
                except (TypeError, ValueError, OSError):
                    watched_at = None
            records.append(
                WatchRecord(
                    grandparent_rating_key=(
                        str(row["grandparent_rating_key"])
                        if row.get("grandparent_rating_key")
                        else None
                    ),
                    title=row.get("grandparent_title"),
                    watched_at=watched_at,
                )
            )
        return records

    async def last_watched_by_show(self, length: int = 2000) -> dict[str, str]:
        """{series plex rating_key: newest watched_at}. History is desc, so first wins."""
        newest: dict[str, str] = {}
        for record in await self.show_history(length=length):
            key = record.grandparent_rating_key
            if key and record.watched_at and key not in newest:
                newest[key] = record.watched_at
        return newest
