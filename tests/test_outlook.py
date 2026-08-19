from datetime import UTC, datetime, timedelta

import pytest

from app.outlook import (
    ANNOUNCED,
    BETWEEN_SEASONS,
    CANCELLED,
    DATED,
    DORMANT,
    ENDED,
    IN_PRODUCTION,
    UNKNOWN,
    compute_outlook,
    parse_dt,
)

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def iso(days: int) -> str:
    return (NOW + timedelta(days=days)).isoformat()


def test_future_episode_wins():
    assert compute_outlook(next_airing=iso(3), now=NOW) == DATED


def test_past_next_airing_does_not_count_as_dated():
    """Stale data: nextAiring in the past must fall through, not read as dated."""
    assert compute_outlook(
        next_airing=iso(-5), previous_airing=iso(-5), sonarr_status="continuing", now=NOW
    ) == BETWEEN_SEASONS


def test_dated_beats_a_stale_ended_status():
    """A finale still to air outranks TMDB calling the show Ended."""
    assert compute_outlook(next_airing=iso(7), tmdb_status="Ended", now=NOW) == DATED


def test_announced_when_metadata_has_an_unaired_season():
    assert (
        compute_outlook(latest_season=3, latest_aired_season=2, now=NOW) == ANNOUNCED
    )


def test_no_announcement_when_seasons_are_level():
    assert compute_outlook(latest_season=2, latest_aired_season=2, now=NOW) == UNKNOWN


def test_in_production_flag():
    assert compute_outlook(in_production=True, now=NOW) == IN_PRODUCTION


def test_in_production_status_string():
    assert compute_outlook(tmdb_status="In Production", now=NOW) == IN_PRODUCTION


def test_cancelled_distinguished_from_ended():
    assert compute_outlook(tmdb_status="Canceled", now=NOW) == CANCELLED
    assert compute_outlook(tmdb_status="Ended", now=NOW) == ENDED


def test_sonarr_ended_is_enough_without_tmdb():
    assert compute_outlook(sonarr_status="ended", now=NOW) == ENDED


def test_ended_outranks_hiatus():
    """A finished show must never be described as merely on hiatus."""
    assert (
        compute_outlook(
            previous_airing=iso(-30), sonarr_status="continuing", tmdb_status="Ended", now=NOW
        )
        == ENDED
    )


def test_recent_airing_is_between_seasons():
    assert (
        compute_outlook(previous_airing=iso(-60), sonarr_status="continuing", now=NOW)
        == BETWEEN_SEASONS
    )


def test_long_silence_is_dormant():
    """The whole point: TVDB says continuing, reality says it's over."""
    assert (
        compute_outlook(
            previous_airing=iso(-700), sonarr_status="continuing", now=NOW
        )
        == DORMANT
    )


def test_dormant_threshold_is_configurable():
    two_years_ago = iso(-730)
    assert (
        compute_outlook(previous_airing=two_years_ago, dormant_months=36, now=NOW)
        == BETWEEN_SEASONS
    )
    assert (
        compute_outlook(previous_airing=two_years_ago, dormant_months=12, now=NOW)
        == DORMANT
    )


def test_in_production_beats_dormancy():
    """Revived after years off: filming outranks a long silence."""
    assert (
        compute_outlook(previous_airing=iso(-900), in_production=True, now=NOW)
        == IN_PRODUCTION
    )


def test_never_aired_but_planned():
    assert compute_outlook(tmdb_status="Planned", now=NOW) == ANNOUNCED
    assert compute_outlook(sonarr_status="upcoming", now=NOW) == ANNOUNCED


def test_no_signal_at_all():
    assert compute_outlook(now=NOW) == UNKNOWN


@pytest.mark.parametrize(
    "value",
    ["2026-08-22T01:00:00Z", "2026-08-22T01:00:00+00:00", "2026-08-22T01:00:00"],
)
def test_parse_dt_accepts_sonarr_and_our_own_formats(value):
    dt = parse_dt(value)
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2026


@pytest.mark.parametrize("value", [None, "", "not a date", "2026-13-45"])
def test_parse_dt_degrades_rather_than_raising(value):
    assert parse_dt(value) is None


def test_unparseable_next_airing_does_not_crash():
    assert compute_outlook(next_airing="garbage", sonarr_status="ended", now=NOW) == ENDED
