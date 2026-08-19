"""Season outlook — is a new season actually coming? (SPEC §10)

Sonarr's `status: continuing` means only "TVDB hasn't marked this ended".
Nobody updates a quietly cancelled show, so it stays `continuing` forever.
This module combines several weaker signals into one honest verdict.

Kept as a pure function of its inputs — no DB, no network — so the ladder
is testable in isolation. See tests/test_outlook.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

DATED: Final = "dated"
ANNOUNCED: Final = "announced"
IN_PRODUCTION: Final = "in_production"
CANCELLED: Final = "cancelled"
ENDED: Final = "ended"
BETWEEN_SEASONS: Final = "between_seasons"
DORMANT: Final = "dormant"
UNKNOWN: Final = "unknown"

#: Outlooks that mean "there is a future worth pinning for".
HAS_FUTURE: Final = frozenset({DATED, ANNOUNCED, IN_PRODUCTION, BETWEEN_SEASONS})

#: Display metadata. Order here is the order facets appear in the UI.
LABELS: Final[dict[str, str]] = {
    DATED: "Dated",
    ANNOUNCED: "Announced",
    IN_PRODUCTION: "Filming",
    BETWEEN_SEASONS: "Hiatus",
    DORMANT: "Probably over",
    CANCELLED: "Cancelled",
    ENDED: "Ended",
    UNKNOWN: "Unknown",
}

_DAYS_PER_MONTH: Final = 30.44


def parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp, tolerating a trailing Z and naive values.

    Sonarr sends `2026-08-22T01:00:00Z`; our own rows are `+00:00`. Anything
    unparseable is treated as absent rather than raising — bad upstream data
    should degrade the badge, not break the page.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def compute_outlook(
    *,
    next_airing: str | None = None,
    previous_airing: str | None = None,
    latest_season: int | None = None,
    latest_aired_season: int | None = None,
    sonarr_status: str | None = None,
    tmdb_status: str | None = None,
    in_production: bool | None = None,
    now: datetime | None = None,
    hiatus_months: int = 9,
    dormant_months: int = 18,
) -> str:
    """Return the outlook for one series. First matching rung wins.

    Ordering matters and is not the same as "most to least specific":

    - `dated` and `announced` come first because concrete scheduling data
      beats any status string. A show TMDB calls "Ended" while its finale is
      still unaired is `dated`, not `ended`.
    - `cancelled`/`ended` come *before* the hiatus/dormant rungs, so a
      genuinely finished show is never described as "on hiatus".
    """
    now = now or datetime.now(UTC)
    tmdb = (tmdb_status or "").strip().lower()
    sonarr = (sonarr_status or "").strip().lower()

    # 1. A dated future episode is the strongest possible signal.
    nxt = parse_dt(next_airing)
    if nxt and nxt > now:
        return DATED

    # 2. Metadata knows about a season beyond the last one that aired, but
    #    hasn't got dates for it yet.
    if (
        latest_season is not None
        and latest_aired_season is not None
        and latest_season > latest_aired_season
    ):
        return ANNOUNCED

    # 3. TMDB says cameras are rolling.
    if in_production or tmdb == "in production":
        return IN_PRODUCTION

    # 4/5. Definitively over. TMDB distinguishes these two; TVDB does not.
    if tmdb == "canceled":
        return CANCELLED
    if tmdb == "ended" or sonarr == "ended":
        return ENDED

    # 6/7. Still nominally continuing. How long since anything aired?
    prev = parse_dt(previous_airing)
    if prev:
        age = now - prev
        if age <= timedelta(days=hiatus_months * _DAYS_PER_MONTH):
            return BETWEEN_SEASONS
        if age > timedelta(days=dormant_months * _DAYS_PER_MONTH):
            return DORMANT
        # Between the two thresholds: long gap, but not yet suspicious.
        return BETWEEN_SEASONS

    # Nothing has ever aired and nothing is scheduled.
    if tmdb in {"planned", "pilot"} or sonarr == "upcoming":
        return ANNOUNCED

    return UNKNOWN


def badge(outlook: str | None, next_airing: str | None = None) -> str:
    """Short human label for a poster card."""
    if outlook == DATED:
        dt = parse_dt(next_airing)
        if dt:
            return f"▸ {dt.strftime('%-d %b')}"
        return "▸ dated"
    return LABELS.get(outlook or UNKNOWN, "—")
