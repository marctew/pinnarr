"""Human-readable names for the values we store.

The database stores `in_production` and `com.plexapp.agents.thetvdb` because
those are what the upstreams call them. Neither belongs in a filter rail. One
place for the mapping so the calendar, library and series page cannot drift
from each other.
"""

from __future__ import annotations

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
