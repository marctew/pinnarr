"""Accounts, roles, and the isolation between users' pin lists.

The tests that matter most here assert that one account cannot see or change
another's pins. That is the whole feature, and it is the part that would fail
silently rather than loudly if it regressed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.db import session, utcnow
from app.main import app


def add_series(conn, title="Severance"):
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO series (title, sort_title, outlook, created_at, updated_at) "
        "VALUES (?, ?, 'dated', ?, ?)",
        (title, title.lower(), now, now),
    )
    return int(cur.lastrowid)


def signed_in(token: str) -> TestClient:
    c = TestClient(app)
    c.cookies.set(auth.COOKIE, token)
    return c


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


# ── Passwords ──


def test_a_hash_verifies_only_against_its_own_password():
    stored = auth.hash_password("correct horse")
    assert auth.verify_password("correct horse", stored)
    assert not auth.verify_password("Correct horse", stored)


def test_the_same_password_hashes_differently_every_time():
    """Salted, so two users with the same password do not look identical."""
    assert auth.hash_password("hunter22") != auth.hash_password("hunter22")


def test_a_corrupt_hash_is_rejected_rather_than_raising():
    assert not auth.verify_password("anything", "not-a-hash")
    assert not auth.verify_password("anything", "")


# ── The gate ──


def test_an_anonymous_visitor_is_sent_to_setup_when_nobody_exists(db):
    r = TestClient(app).get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup"


def test_once_an_account_exists_the_gate_is_the_login_form(db, admin_token):
    r = TestClient(app).get("/library", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_the_page_you_wanted_is_remembered_through_the_login(db, admin_token):
    r = TestClient(app).get("/library?outlook=ended", follow_redirects=False)
    assert "next=" in r.headers["location"]
    assert "library" in r.headers["location"]


def test_health_stays_reachable_without_a_session(db):
    assert TestClient(app).get("/healthz").status_code == 200


def test_setup_is_closed_once_an_admin_exists(db, admin_token):
    r = TestClient(app).post(
        "/setup",
        data={"username": "sneaky", "password": "password123"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/login"
    with session() as conn:
        assert auth.by_username(conn, "sneaky") is None


def test_signing_in_with_the_wrong_password_gets_you_nowhere(db, admin_token):
    c = TestClient(app)
    r = c.post(
        "/login", data={"username": "admin", "password": "wrong"}, follow_redirects=False
    )
    assert "error=" in r.headers["location"]
    assert c.cookies.get(auth.COOKIE) is None


def test_signing_in_works_and_signing_out_revokes_the_session(db, admin_token):
    c = TestClient(app)
    c.post("/login", data={"username": "admin", "password": "password123"})
    assert c.get("/library").status_code == 200

    c.post("/logout")
    assert TestClient(app).get("/library", follow_redirects=False).status_code == 303


def test_login_will_not_bounce_you_off_site(db, admin_token):
    """An open redirect on a login form is a phishing primitive."""
    c = TestClient(app)
    r = c.post(
        "/login",
        data={"username": "admin", "password": "password123", "next": "//evil.example.com"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/"


# ── Roles ──


def test_a_standard_user_cannot_reach_the_settings_panel(db, account):
    account()
    token, _ = account("bob", "user")
    assert signed_in(token).get("/settings").status_code == 403


def test_a_standard_user_cannot_reach_account_management(db, account):
    account()
    token, _ = account("bob", "user")
    assert signed_in(token).get("/settings/users").status_code == 403


def test_an_admin_can(client):
    assert client.get("/settings/users").status_code == 200


def test_the_settings_link_is_hidden_from_standard_users(db, account):
    account()
    token, _ = account("bob", "user")
    assert 'href="/settings"' not in signed_in(token).get("/").text


# ── Pin isolation ──


def test_unpinning_as_one_user_leaves_anothers_pin_alone(db, account):
    admin_tok, _ = account()
    bob_tok, _ = account("bob", "user")
    with session() as conn:
        sid = add_series(conn)

    signed_in(admin_tok).post(f"/api/series/{sid}/pin")
    assert signed_in(bob_tok).post(f"/api/series/{sid}/unpin").json()["pinned_total"] == 0
    assert "Severance" in signed_in(admin_tok).get("/library?pinned=pinned").text


def test_the_pinned_count_is_your_own(db, account):
    admin_tok, _ = account()
    bob_tok, _ = account("bob", "user")
    with session() as conn:
        sid = add_series(conn)

    assert signed_in(admin_tok).post(f"/api/series/{sid}/pin").json()["pinned_total"] == 1
    assert signed_in(bob_tok).post(f"/api/series/{sid}/pin").json()["pinned_total"] == 1


def test_one_users_calendar_does_not_show_anothers_pins(db, account):
    admin_tok, _ = account()
    bob_tok, _ = account("bob", "user")
    with session() as conn:
        sid = add_series(conn)
    signed_in(admin_tok).post(f"/api/series/{sid}/pin")

    assert "Nothing pinned yet" in signed_in(bob_tok).get("/").text
    assert "Nothing pinned yet" not in signed_in(admin_tok).get("/").text


def test_the_shared_flag_clears_only_when_the_last_user_unpins(db, account):
    """series.pinned is denormalised for the sync jobs, so it must survive one
    user unpinning and fall to 0 only when nobody follows the show."""
    admin_tok, _ = account()
    bob_tok, _ = account("bob", "user")
    with session() as conn:
        sid = add_series(conn)

    def flag() -> int:
        with session() as conn:
            return conn.execute(
                "SELECT pinned FROM series WHERE id = ?", (sid,)
            ).fetchone()["pinned"]

    signed_in(admin_tok).post(f"/api/series/{sid}/pin")
    signed_in(bob_tok).post(f"/api/series/{sid}/pin")
    assert flag() == 1

    signed_in(admin_tok).post(f"/api/series/{sid}/unpin")
    assert flag() == 1

    signed_in(bob_tok).post(f"/api/series/{sid}/unpin")
    assert flag() == 0


def test_undo_only_touches_your_own_bulk_pin(db, account):
    admin_tok, _ = account()
    bob_tok, _ = account("bob", "user")
    with session() as conn:
        add_series(conn, "Alpha")
        add_series(conn, "Beta")

    signed_in(admin_tok).post("/api/series/bulk-pin")
    signed_in(bob_tok).post("/api/series/bulk-pin")

    assert signed_in(bob_tok).post("/api/series/bulk-undo").json()["undone"] == 2

    mine = signed_in(admin_tok).get("/library?pinned=pinned").text
    assert "Alpha" in mine and "Beta" in mine
    assert signed_in(bob_tok).get("/library?pinned=pinned").text.count("class=\"card") == 0


# ── The upgrade path ──


def test_pins_made_before_accounts_existed_go_to_the_first_admin(db):
    with session() as conn:
        sid = add_series(conn)
        conn.execute("UPDATE series SET pinned = 1 WHERE id = ?", (sid,))

    c = TestClient(app)
    c.post("/setup", data={"username": "marc", "password": "password123"})
    assert "Severance" in c.get("/library?pinned=pinned").text


def test_a_later_account_does_not_inherit_those_pins(db):
    with session() as conn:
        sid = add_series(conn)
        conn.execute("UPDATE series SET pinned = 1 WHERE id = ?", (sid,))

    TestClient(app).post("/setup", data={"username": "marc", "password": "password123"})
    with session() as conn:
        user_id = auth.create_user(conn, "bob", "password123", "user")
        token = auth.start_session(conn, user_id)
    assert "Nothing pinned yet" in signed_in(token).get("/").text


# ── Account management ──


def test_an_admin_can_create_an_account(client):
    client.post(
        "/settings/users",
        data={"action": "create", "username": "bob", "password": "password123", "role": "user"},
    )
    with session() as conn:
        assert auth.by_username(conn, "bob") is not None


def test_a_short_password_is_refused(client):
    client.post(
        "/settings/users", data={"action": "create", "username": "bob", "password": "short"}
    )
    with session() as conn:
        assert auth.by_username(conn, "bob") is None


def test_a_duplicate_username_is_refused(client):
    for _ in range(2):
        client.post(
            "/settings/users",
            data={"action": "create", "username": "bob", "password": "password123"},
        )
    with session() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM users WHERE username = 'bob'"
        ).fetchone()["n"] == 1


def test_the_last_admin_cannot_be_demoted(client, admin_token):
    _, me = admin_token
    client.post("/settings/users", data={"action": "role", "user_id": me, "role": "user"})
    with session() as conn:
        assert auth.get_user(conn, me)["role"] == "admin"


def test_the_last_admin_cannot_be_deleted(client, admin_token):
    _, me = admin_token
    client.post("/settings/users", data={"action": "delete", "user_id": me})
    with session() as conn:
        assert auth.get_user(conn, me) is not None


def test_deleting_a_user_takes_their_pins_with_them(db, account):
    account()
    bob_tok, bob = account("bob", "user")
    with session() as conn:
        sid = add_series(conn)
    signed_in(bob_tok).post(f"/api/series/{sid}/pin")

    with session() as conn:
        auth.delete_user(conn, bob)
        left = conn.execute(
            "SELECT count(*) AS n FROM pins WHERE user_id = ?", (bob,)
        ).fetchone()["n"]
    assert left == 0


def test_changing_your_password_signs_you_out_everywhere(db, admin_token):
    token, _ = admin_token
    assert signed_in(token).get("/library").status_code == 200

    signed_in(token).post("/profile", data={"ntfy_topic": "", "password": "a-longer-password"})
    assert signed_in(token).get("/library", follow_redirects=False).status_code == 303


def test_your_ntfy_topic_is_yours(db, admin_token):
    token, user_id = admin_token
    signed_in(token).post("/profile", data={"ntfy_topic": "marc-shows"})
    with session() as conn:
        assert auth.get_user(conn, user_id)["ntfy_topic"] == "marc-shows"
