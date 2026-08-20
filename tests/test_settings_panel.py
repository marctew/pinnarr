"""The admin panel.

The tests that matter most here are the ones about secrets: a settings page
that echoes your Plex token into the HTML, or wipes it because you saved the
form without retyping it, is worse than no panel at all.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import get_settings, save_settings
from app.main import app

TOKEN = "plex-token-do-not-leak"


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


def test_the_panel_is_reachable_from_every_page(client):
    # The root is the calendar now, so the nav is the only way in.
    assert 'href="/settings"' in client.get("/").text


def test_the_panel_renders(client):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Season outlook" in r.text


def test_a_saved_secret_never_reaches_the_browser(client):
    save_settings({"plex_token": TOKEN})
    body = client.get("/settings").text
    assert TOKEN not in body
    assert "saved — leave blank to keep" in body


def test_saving_the_form_persists_values(client):
    client.post(
        "/settings",
        data={"plex_url": "http://plex.lan:32400/", "plex_tv_sections": "2, 5"},
    )
    s = get_settings()
    assert s.plex_url == "http://plex.lan:32400"
    assert s.plex_tv_sections == [2, 5]


def test_an_untouched_password_box_does_not_wipe_the_secret(client):
    save_settings({"plex_token": TOKEN})
    client.post("/settings", data={"plex_url": "http://plex.lan:32400", "plex_token": ""})
    assert get_settings().plex_token == TOKEN


def test_a_new_secret_replaces_the_old_one(client):
    save_settings({"plex_token": TOKEN})
    client.post("/settings", data={"plex_token": "a-new-token"})
    assert get_settings().plex_token == "a-new-token"


def test_unticking_a_checkbox_turns_the_setting_off(client):
    assert get_settings().digest_enabled is True
    # A browser omits an unticked box entirely; the hidden companion is what
    # distinguishes "unticked" from "not on this form".
    client.post("/settings", data={"digest_enabled": "false"})
    assert get_settings().digest_enabled is False


def test_ticking_a_checkbox_turns_it_back_on(client):
    save_settings({"digest_enabled": "false"})
    client.post("/settings", data={"digest_enabled": ["false", "true"]})
    assert get_settings().digest_enabled is True


def test_a_rejected_value_reports_back_and_changes_nothing(client):
    r = client.post("/settings", data={"hiatus_months": "banana"})
    assert "hiatus_months" in r.text
    assert get_settings().hiatus_months == 9


def test_changing_the_timezone_rebuilds_the_schedule(client):
    before = client.app.state.scheduler
    client.post("/settings", data={"tz": "America/New_York"})
    assert client.app.state.scheduler is not before
    assert client.app.state.scheduler.running


def test_saving_something_unscheduled_leaves_the_scheduler_alone(client):
    before = client.app.state.scheduler
    client.post("/settings", data={"plex_url": "http://plex.lan:32400"})
    assert client.app.state.scheduler is before


@pytest.mark.parametrize("service", ["plex", "sonarr", "tautulli", "tmdb", "ntfy"])
def test_testing_an_unconfigured_service_fails_politely(client, service):
    body = client.post(f"/api/settings/test/{service}").json()
    assert body["ok"] is False
    assert "saved yet" in body["message"]


def test_testing_an_unknown_service_is_not_an_error(client):
    body = client.post("/api/settings/test/nope").json()
    assert body["ok"] is False
    assert "Unknown service" in body["message"]


def test_every_setting_has_a_control_on_the_page(client):
    """A settings field with no control is invisible and unreachable. This
    caught show_unmonitored shipping without its checkbox."""
    from app.config import SECRET_FIELDS, Settings

    body = client.get("/settings").text
    # Radarr is v1.5 and deliberately not on the form yet.
    skip = {f for f in Settings.model_fields if f.startswith("radarr_")}
    for field in Settings.model_fields:
        if field in skip or field in SECRET_FIELDS:
            continue
        assert f'name="{field}"' in body, f"{field} has no control on /settings"


def test_the_unmonitored_toggle_is_on_the_page(client):
    body = client.get("/settings").text
    assert 'name="show_unmonitored"' in body
    assert "Show episodes Sonarr isn't monitoring" in body


def test_the_unmonitored_toggle_round_trips_through_the_form(client):
    from app.config import get_settings

    assert get_settings().show_unmonitored is False
    client.post("/settings", data={"show_unmonitored": ["false", "true"]})
    assert get_settings().show_unmonitored is True

    client.post("/settings", data={"show_unmonitored": "false"})
    assert get_settings().show_unmonitored is False
