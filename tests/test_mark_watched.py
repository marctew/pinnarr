"""Marking watched from Pinnarr, and pulling it back on demand.

Both go through Plex rather than around it. Plex is authoritative for anyone
with a personal token — the sweep records what it says and clears what it
does not — so a mark that only ever reached Pinnarr would survive until the
next sweep and then vanish. A lie with a timer on it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth, watching
from app.clients.http import UpstreamError
from app.clients.plex import EpisodeView, PlexClient
from app.config import save_settings
from app.db import session
from app.main import app
from tests.factories import iso, make_episode, make_series, pin, watch

PLEX = "http://plex.lan:32400"


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        save_settings({"plex_url": PLEX, "plex_token": "server"})
        yield c


@pytest.fixture
def plex(monkeypatch):
    """A Plex that remembers what it was told and answers accordingly."""
    state = {
        "watched": set(),        # episode rating keys Plex considers watched
        "scrobbles": [],         # (key, watched) in order
        "tokens": [],            # whose token each call used
        "fail": False,
    }

    async def fake_scrobble(self, rating_key, *, watched):
        if state["fail"]:
            raise UpstreamError("plex", "nope")
        state["tokens"].append(self.token)
        state["scrobbles"].append((rating_key, watched))
        # A show-level key applies downward, which is why one call can do
        # what several hundred would.
        keys = {"901", "902"} if rating_key == "55" else {rating_key}
        if watched:
            state["watched"] |= keys
        else:
            state["watched"] -= keys

    async def fake_view_state(self, rating_key):
        if state["fail"]:
            raise UpstreamError("plex", "nope")
        state["tokens"].append(self.token)
        return {
            (1, 1): EpisodeView(watched="901" in state["watched"], rating_key="901",
                                viewed_at="2026-01-01T20:00:00+00:00"),
            (1, 2): EpisodeView(watched="902" in state["watched"], rating_key="902",
                                viewed_at="2026-01-02T20:00:00+00:00"),
        }

    monkeypatch.setattr(PlexClient, "scrobble", fake_scrobble)
    monkeypatch.setattr(PlexClient, "view_state", fake_view_state)
    return state


def seed(conn, user_id, *, token="mine", plex_keys=True):
    if token:
        conn.execute("UPDATE users SET plex_token = ? WHERE id = ?", (token, user_id))
    sid = make_series(conn, "Silo", plex_rating_key="55")
    pin(conn, user_id, sid)
    ids = []
    for number in (1, 2):
        ids.append(
            make_episode(
                conn, sid, season=1, episode=number, has_file=1, in_plex=1,
                air_date_utc=iso(days=-10),
                plex_rating_key=f"90{number}" if plex_keys else None,
            )
        )
    return sid, ids


def watched_count():
    with session() as conn:
        return conn.execute("SELECT count(*) AS n FROM episode_watches").fetchone()["n"]


# ── Refreshing from Plex ──


async def test_refresh_pulls_what_plex_says(client, admin_token, plex):
    _, user_id = admin_token
    with session() as conn:
        sid, _ = seed(conn, user_id)
    plex["watched"].add("901")

    r = client.post(f"/api/series/{sid}/refresh-watched")
    assert r.status_code == 200
    assert r.json()["marked"] == 1
    assert watched_count() == 1


async def test_refresh_clears_what_plex_no_longer_says(client, admin_token, plex):
    """The same reconciliation the sweep does, on demand."""
    _, user_id = admin_token
    with session() as conn:
        sid, ids = seed(conn, user_id)
        watch(conn, user_id, ids[0])
    assert watched_count() == 1

    r = client.post(f"/api/series/{sid}/refresh-watched")
    assert r.json()["cleared"] == 1
    assert watched_count() == 0


async def test_refresh_says_when_nothing_changed(client, admin_token, plex):
    _, user_id = admin_token
    with session() as conn:
        sid, _ = seed(conn, user_id)
    assert "in step" in client.post(f"/api/series/{sid}/refresh-watched").json()["detail"]


async def test_refresh_uses_your_token_not_the_servers(client, admin_token, plex):
    """View state is per Plex account. The server token would answer for
    whoever owns the server, which is not necessarily you."""
    _, user_id = admin_token
    with session() as conn:
        sid, _ = seed(conn, user_id, token="mine")
    client.post(f"/api/series/{sid}/refresh-watched")
    assert set(plex["tokens"]) == {"mine"}


# ── Marking, which writes to Plex ──


async def test_marking_an_episode_tells_plex(client, admin_token, plex):
    _, user_id = admin_token
    with session() as conn:
        _, ids = seed(conn, user_id)

    r = client.post(f"/api/episodes/{ids[0]}/watched", data={"watched": "true"})
    assert r.status_code == 200
    assert plex["scrobbles"] == [("901", True)]
    assert watched_count() == 1


async def test_unmarking_an_episode_tells_plex(client, admin_token, plex):
    _, user_id = admin_token
    with session() as conn:
        _, ids = seed(conn, user_id)
    plex["watched"].add("901")
    client.post(f"/api/episodes/{ids[0]}/watched", data={"watched": "false"})
    assert plex["scrobbles"] == [("901", False)]
    assert watched_count() == 0


async def test_marking_a_whole_series_is_one_call(client, admin_token, plex):
    """Plex applies a show-level scrobble downward, so a forty-episode run
    does not become forty requests."""
    _, user_id = admin_token
    with session() as conn:
        sid, _ = seed(conn, user_id)
    client.post(f"/api/series/{sid}/watched", data={"watched": "true"})
    assert plex["scrobbles"] == [("55", True)]
    assert watched_count() == 2


async def test_marking_a_season_walks_its_episodes(client, admin_token, plex):
    _, user_id = admin_token
    with session() as conn:
        sid, _ = seed(conn, user_id)
    client.post(f"/api/series/{sid}/watched", data={"watched": "true", "season": "1"})
    assert plex["scrobbles"] == [("901", True), ("902", True)]


async def test_the_local_state_is_read_back_not_assumed(
    client, admin_token, plex, monkeypatch
):
    """Believing a write is how the watchlist sync ended up unpinning things.
    If Plex accepts the call and quietly does nothing — which it does — the
    page must not claim otherwise."""
    _, user_id = admin_token
    with session() as conn:
        _, ids = seed(conn, user_id)

    async def accepts_and_ignores(self, rating_key, *, watched):
        return None

    monkeypatch.setattr(PlexClient, "scrobble", accepts_and_ignores)
    r = client.post(f"/api/episodes/{ids[0]}/watched", data={"watched": "true"})
    assert r.status_code == 200
    assert watched_count() == 0


# ── When it cannot ──


async def test_without_your_own_token_it_refuses_rather_than_lying(client, admin_token):
    """A local-only mark would be cleared by the next sweep, so recording one
    is worse than saying no."""
    _, user_id = admin_token
    with session() as conn:
        sid, ids = seed(conn, user_id, token=None)

    r = client.post(f"/api/episodes/{ids[0]}/watched", data={"watched": "true"})
    assert r.status_code == 409
    assert "your own Plex token" in r.json()["detail"]
    assert watched_count() == 0


async def test_a_series_plex_never_matched_is_refused(client, admin_token, plex):
    _, user_id = admin_token
    with session() as conn:
        conn.execute("UPDATE users SET plex_token = 'mine' WHERE id = ?", (user_id,))
        sid = make_series(conn, "Unmatched", plex_rating_key=None)
        pin(conn, user_id, sid)
    r = client.post(f"/api/series/{sid}/refresh-watched")
    assert r.status_code == 409
    assert "matched in Plex" in r.json()["detail"]


async def test_a_season_with_no_plex_ids_yet_says_so(client, admin_token, plex):
    """Rather than silently scrobbling nothing and reporting success."""
    _, user_id = admin_token
    with session() as conn:
        sid, _ = seed(conn, user_id, plex_keys=False)
    r = client.post(f"/api/series/{sid}/watched", data={"watched": "true", "season": "1"})
    assert r.status_code == 409
    assert "refresh from plex first" in r.json()["detail"].lower()


async def test_plex_refusing_is_reported_not_swallowed(client, admin_token, plex):
    _, user_id = admin_token
    with session() as conn:
        _, ids = seed(conn, user_id)
    plex["fail"] = True
    r = client.post(f"/api/episodes/{ids[0]}/watched", data={"watched": "true"})
    assert r.status_code == 409
    assert "Plex said no" in r.json()["detail"]


async def test_an_unknown_episode_is_a_404(client, admin_token, plex):
    assert client.post("/api/episodes/99999/watched").status_code == 404


async def test_one_persons_mark_is_not_anothers(db, account, plex):
    _, marc = account()
    _, bob = account("bob", "user")
    with session() as conn:
        sid, _ = seed(conn, marc)
        pin(conn, bob, sid)
    save_settings({"plex_url": PLEX, "plex_token": "server"})
    plex["watched"].add("901")

    await watching.refresh(marc, sid)
    with session() as conn:
        owners = {
            int(r["user_id"])
            for r in conn.execute("SELECT user_id FROM episode_watches")
        }
    assert owners == {marc}


# ── The page ──


def test_the_buttons_are_on_the_series_page(client, admin_token, plex):
    _, user_id = admin_token
    with session() as conn:
        sid, _ = seed(conn, user_id)
    body = client.get(f"/series/{sid}").text
    assert "Refresh watched from Plex" in body
    assert "Mark all watched" in body
    assert 'class="tick"' in body
