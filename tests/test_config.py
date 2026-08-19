"""The settings layer, now that the database owns it rather than the env.

The env is deliberately not consulted for these fields, so several of these
tests set environment variables and assert they are ignored.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import get_settings, save_settings
from app.db import all_settings


def test_defaults_apply_when_nothing_is_stored(db):
    s = get_settings()
    assert s.plex_url == ""
    assert s.plex_tv_sections == []
    assert s.digest_cron == "0 8 * * 1"
    assert s.hiatus_months == 9


def test_saving_round_trips_through_sqlite(db):
    save_settings({"plex_url": "http://plex.lan:32400", "plex_token": "abc123"})
    s = get_settings()
    assert s.plex_url == "http://plex.lan:32400"
    assert s.plex_token == "abc123"
    assert s.plex_configured is True


def test_environment_is_ignored_for_panel_owned_fields(db, monkeypatch):
    monkeypatch.setenv("PLEX_URL", "http://from-the-env:32400")
    get_settings.cache_clear()
    assert get_settings().plex_url == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", []), ("2", [2]), ("2,5", [2, 5]), ("2, 5", [2, 5]), ("2,5,", [2, 5])],
)
def test_section_ids_accept_the_shapes_a_form_produces(db, raw, expected):
    save_settings({"plex_tv_sections": raw})
    assert get_settings().plex_tv_sections == expected


def test_lists_are_stored_as_plain_csv_not_json(db):
    save_settings({"plex_tv_sections": "2, 5"})
    assert all_settings()["plex_tv_sections"] == "2,5"


def test_booleans_survive_the_string_round_trip(db):
    save_settings({"digest_enabled": "false"})
    assert get_settings().digest_enabled is False
    assert all_settings()["digest_enabled"] == "false"

    save_settings({"digest_enabled": "true"})
    assert get_settings().digest_enabled is True


def test_a_partial_save_leaves_everything_else_alone(db):
    save_settings({"plex_url": "http://plex.lan:32400", "plex_token": "secret"})
    save_settings({"sonarr_url": "http://sonarr.lan:8989"})
    s = get_settings()
    assert s.plex_token == "secret"
    assert s.sonarr_url == "http://sonarr.lan:8989"


def test_urls_lose_their_trailing_slash(db):
    save_settings({"sonarr_url": "http://sonarr.lan:8989/"})
    assert get_settings().sonarr_url == "http://sonarr.lan:8989"


def test_a_bad_value_is_rejected_whole(db):
    save_settings({"hiatus_months": "9"})
    with pytest.raises(ValidationError):
        save_settings({"hiatus_months": "not a number", "dormant_months": "24"})

    # Neither field should have been written — a half-applied form is worse
    # than a rejected one.
    s = get_settings()
    assert s.hiatus_months == 9
    assert s.dormant_months == 18


def test_unknown_keys_are_ignored_rather_than_stored(db):
    save_settings({"plex_url": "http://plex.lan:32400", "nonsense": "x"})
    assert "nonsense" not in all_settings()
