"""Human-readable names for the values we store.

The database stores `in_production` and `com.plexapp.agents.thetvdb` because
those are what the upstreams call them. Neither belongs in a filter rail. One
place for the mapping so the calendar, library and series page cannot drift
from each other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

OUTLOOK: Final[dict[str, str]] = {
    "dated": "Dated",
    "announced": "Announced",
    "in_production": "Filming",
    "between_seasons": "On hiatus",
    "dormant": "Probably over",
    "cancelled": "Cancelled",
    "ended": "Ended",
    "unknown": "Unknown",
}

#: Short forms for the calendar and the poster grid, where a badge sits under
#: artwork and has no room to explain itself.
OUTLOOK_BADGE: Final[dict[str, str]] = {
    "dated": "▸ dated",
    "announced": "announced",
    "in_production": "filming",
    "between_seasons": "hiatus",
    "dormant": "⚠ probably over",
    "cancelled": "cancelled",
    "ended": "ended",
    "unknown": "—",
}

SONARR_STATUS: Final[dict[str, str]] = {
    "continuing": "Continuing",
    "ended": "Ended",
    "upcoming": "Not yet aired",
    # Sonarr keeps the row and marks it deleted; without this it reads as a
    # state of the show rather than a state of your Sonarr library.
    "deleted": "Removed from Sonarr",
}


def outlook(value: str | None) -> str:
    return OUTLOOK.get(value or "unknown", (value or "—").replace("_", " ").capitalize())


def outlook_badge(value: str | None) -> str:
    return OUTLOOK_BADGE.get(value or "unknown", "—")


def sonarr_status(value: str | None) -> str:
    return SONARR_STATUS.get(value or "", (value or "—").capitalize())


def relative_day(day: object, today: object) -> str:
    """"in 3 days" rather than a date you have to subtract in your head."""
    delta = (day - today).days  # type: ignore[operator]
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    return f"in {delta} days" if delta > 0 else f"{-delta} days ago"


def duration(minutes: object) -> str:
    """"2h 45m". The question at 9pm is not what is available, it is what
    can be finished."""
    try:
        total = int(minutes or 0)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, mins = divmod(total, 60)
    if not hours:
        return f"{mins}m"
    return f"{hours}h" if not mins else f"{hours}h {mins}m"


def since(timestamp: object) -> str:
    """How long ago, coarsely. "4h", "2d" — enough to judge a stalled grab."""
    if not timestamp:
        return "a while"
    try:
        then = datetime.fromisoformat(str(timestamp))
    except (TypeError, ValueError):
        return "a while"
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - then
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{max(1, int(delta.total_seconds() // 60))}m"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"
