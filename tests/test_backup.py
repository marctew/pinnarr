"""Backup and restore.

The test that matters most is the one about TVDB ids: row ids are local to
one database, so a restore into a fresh install that matched on them would
silently attach every pin to the wrong show — and look like it worked.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import auth, backup
from app.config import get_settings, save_settings
from app.db import session
from app.main import app
from tests.factories import make_series


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


def add_series(conn, title="Silo", tvdb_id=12345, year=2023):
    return make_series(conn, title, tvdb_id=tvdb_id, year=year)


# ── Export ──


def test_it_carries_the_three_things_that_cannot_be_rebuilt(client, admin_token):
    _, user_id = admin_token
    save_settings({"plex_url": "http://plex.lan:32400", "plex_token": "secret-token"})
    with session() as conn:
        sid = add_series(conn)
    client.post(f"/api/series/{sid}/pin")

    dump = backup.export()
    assert dump["settings"]["plex_token"] == "secret-token"
    assert [u["username"] for u in dump["users"]] == ["admin"]
    assert dump["pins"][0]["tvdb_id"] == 12345


def test_pins_travel_by_tvdb_id_not_row_id(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        sid = add_series(conn)
    client.post(f"/api/series/{sid}/pin")

    pin = backup.export()["pins"][0]
    assert "series_id" not in pin
    assert pin["tvdb_id"] == 12345


def test_the_download_is_an_attachment(client):
    r = client.get("/api/backup")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert json.loads(r.content)["format"] == backup.FORMAT_VERSION


def test_a_standard_user_cannot_download_the_backup(db, account):
    account()
    token, _ = account("bob", "user")
    c = TestClient(app)
    c.cookies.set(auth.COOKIE, token)
    assert c.get("/api/backup").status_code == 403


# ── Restore ──


def test_a_pin_lands_on_the_right_show_in_a_fresh_database(client, admin_token):
    """Row ids differ between installs; matching on them would attach every
    pin to whatever happened to occupy that id."""
    _, user_id = admin_token
    with session() as conn:
        # Deliberately not id 1: a naive restore would pick the wrong row.
        add_series(conn, "Decoy", tvdb_id=1)
        add_series(conn, "Andor", tvdb_id=2)
        add_series(conn, "Silo", tvdb_id=12345)

    report = backup.restore({
        "format": backup.FORMAT_VERSION,
        "settings": {},
        "users": [],
        "pins": [{"username": "admin", "tvdb_id": 12345, "title": "Silo", "notify": 1}],
    })
    assert report["pins"] == 1
    assert "Silo" in client.get("/library?pinned=pinned").text
    assert "Decoy" not in client.get("/library?pinned=pinned").text


def test_a_show_not_in_the_library_yet_is_reported_not_dropped(client):
    report = backup.restore({
        "format": backup.FORMAT_VERSION,
        "users": [],
        "pins": [{"username": "admin", "tvdb_id": 999, "title": "Not Synced Yet"}],
    })
    assert report["unmatched"] == ["Not Synced Yet"]


def test_a_pin_without_a_tvdb_id_falls_back_to_title_and_year(client):
    with session() as conn:
        add_series(conn, "Silo", tvdb_id=None, year=2023)
    report = backup.restore({
        "format": backup.FORMAT_VERSION,
        "users": [],
        "pins": [{"username": "admin", "tvdb_id": None, "title": "Silo", "year": 2023}],
    })
    assert report["pins"] == 1


def test_restoring_adds_accounts_without_touching_existing_ones(client):
    before = auth.hash_password("whatever")
    report = backup.restore({
        "format": backup.FORMAT_VERSION,
        "users": [
            {"username": "admin", "password_hash": before, "role": "user"},
            {"username": "bob", "password_hash": before, "role": "user",
             "ntfy_topic": "bob-shows"},
        ],
        "pins": [],
    })
    assert report["users"] == 1
    with session() as conn:
        # The existing admin keeps its own password and role.
        assert auth.get_user(conn, 1)["role"] == "admin"
        assert auth.by_username(conn, "bob")["ntfy_topic"] == "bob-shows"


def test_a_restored_account_can_sign_in_with_its_old_password(client):
    backup.restore({
        "format": backup.FORMAT_VERSION,
        "users": [{"username": "bob", "password_hash": auth.hash_password("bobs-password"),
                   "role": "user"}],
        "pins": [],
    })
    assert auth.authenticate("bob", "bobs-password") is not None


def test_settings_are_overwritten(client):
    save_settings({"plex_url": "http://old.lan:32400"})
    backup.restore({
        "format": backup.FORMAT_VERSION,
        "settings": {"plex_url": "http://restored.lan:32400"},
        "users": [],
        "pins": [],
    })
    assert get_settings().plex_url == "http://restored.lan:32400"


def test_the_shared_pinned_flag_is_rebuilt(client, admin_token):
    """series.pinned is derived, so a restore that only wrote pins would leave
    the sync jobs blind to the restored shows."""
    with session() as conn:
        sid = add_series(conn)
    backup.restore({
        "format": backup.FORMAT_VERSION,
        "users": [],
        "pins": [{"username": "admin", "tvdb_id": 12345, "title": "Silo"}],
    })
    with session() as conn:
        assert conn.execute("SELECT pinned FROM series WHERE id = ?", (sid,)).fetchone()["pinned"] == 1


def test_restoring_twice_does_not_duplicate(client):
    with session() as conn:
        add_series(conn)
    payload = {
        "format": backup.FORMAT_VERSION,
        "users": [],
        "pins": [{"username": "admin", "tvdb_id": 12345, "title": "Silo"}],
    }
    assert backup.restore(payload)["pins"] == 1
    assert backup.restore(payload)["pins"] == 0


@pytest.mark.parametrize("payload", [{}, {"format": 99}, [], "nope", None])
def test_something_that_is_not_a_backup_is_refused(client, payload):
    with pytest.raises(ValueError, match="not a Pinnarr backup"):
        backup.restore(payload)


def test_a_round_trip_survives(client, admin_token):
    _, user_id = admin_token
    save_settings({"sonarr_url": "http://sonarr.lan:8989"})
    with session() as conn:
        sid = add_series(conn)
    client.post(f"/api/series/{sid}/pin")

    dump = backup.export()

    with session() as conn:
        conn.execute("DELETE FROM pins")
        conn.execute("UPDATE series SET pinned = 0")
    save_settings({"sonarr_url": ""})

    report = backup.restore(dump)
    assert report["pins"] == 1
    assert get_settings().sonarr_url == "http://sonarr.lan:8989"


def test_uploading_junk_reports_rather_than_500s(client):
    r = client.post("/settings/backup", files={"file": ("x.json", b"not json", "application/json")})
    assert r.status_code == 200
    assert "Could not read that file" in r.text


def test_uploading_nothing_asks_for_a_file(client):
    r = client.post("/settings/backup", data={})
    assert "Choose a backup file first" in r.text
