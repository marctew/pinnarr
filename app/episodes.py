"""Per-episode availability state (SPEC §9).

Derived at render time rather than stored, so it is always correct without a
migration and without a job to keep it fresh.

The `missing` state is the one that earns this module: "aired four days ago
and still isn't here" is precisely what you want to know, and Sonarr buries
it under Wanted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

UPCOMING: Final = "upcoming"
AIRING_TODAY: Final = "airing_today"
AWAITING: Final = "awaiting"
MISSING: Final = "missing"
AVAILABLE: Final = "available"
UNMONITORED: Final = "unmonitored"

#: How long after air time before absence stops being "any minute now" and
#: starts being "something is wrong". Tunable per SPEC §17 if it grates.
GRACE = timedelta(hours=48)

LABELS: Final[dict[str, str]] = {
    UPCOMING: "upcoming",
    AIRING_TODAY: "airs today",
    AWAITING: "expected",
    MISSING: "not arrived",
    AVAILABLE: "in Plex",
    UNMONITORED: "not wanted",
}

MARKS: Final[dict[str, str]] = {
    UPCOMING: "○",
    AIRING_TODAY: "◉",
    AWAITING: "◐",
    MISSING: "✕",
    AVAILABLE: "●",
    UNMONITORED: "–",
}


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _field(row: Any, key: str, default: Any = None) -> Any:
    """Tolerant lookup: rows arrive as sqlite3.Row here and plain dicts in
    tests, and the two disagree about how a missing key fails."""
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def episode_state(
    row: Any, *, now: datetime | None = None, tz: str = "Europe/London"
) -> str:
    """State for one episode row. First match wins.

    Ordering differs from the table in §9, deliberately:

    - `available` is checked first. "Can I watch it" outranks every other
      description, including an episode that airs today and is already in.
    - `airing_today` beats both `upcoming` and `awaiting`, which it overlaps
      with by definition — something airing at 21:00 is upcoming all day, and
      something that aired at 09:00 is technically awaiting by lunchtime.
      Neither is what you want the row to say.
    - `unmonitored` comes next, because an episode Sonarr is not chasing will
      never arrive by design. Calling that `missing` cries wolf about exactly
      the thing you decided you did not want.
    """
    now = now or datetime.now(UTC)
    has_file = bool(_field(row, "has_file")) or bool(_field(row, "in_plex"))
    air = parse(_field(row, "air_date_utc"))

    if has_file:
        return AVAILABLE
    if not _field(row, "monitored", 1):
        return UNMONITORED
    if air is None:
        return UPCOMING

    # Today is a local-calendar question, not a UTC one: Sonarr's air times
    # are UTC and a US show at 02:00 UTC is "tonight" in London terms.
    local = ZoneInfo(tz)
    if air.astimezone(local).date() == now.astimezone(local).date():
        return AIRING_TODAY
    if air > now:
        return UPCOMING
    return AWAITING if now - air <= GRACE else MISSING


def decorate(row: Any, *, now: datetime | None = None, tz: str = "Europe/London") -> dict[str, Any]:
    """A row as the templates want it: state, label and mark alongside."""
    state = episode_state(row, now=now, tz=tz)
    air = parse(row["air_date_utc"])
    return {
        **dict(row),
        "state": state,
        "label": LABELS[state],
        "mark": MARKS[state],
        "air_local": air.astimezone(ZoneInfo(tz)) if air else None,
        "code": f"S{row['season']:02d}E{row['episode']:02d}",
    }
