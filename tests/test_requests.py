"""Asking Overseerr for things you do not own.

Discover has always collected these — store_recommendations keeps every TMDB
suggestion, owned or not — and always thrown them away at query time, on the
grounds that a recommendation you cannot watch is an advert. With somewhere
to send a request that stops being true, so the filter comes off.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import auth
from app.clients.overseerr import OverseerrClient
from app.config import save_settings
from app.db import session, utcnow
from app.jobs.overseerr_sync import sync_requests
from app.main import app
from app.repo import media_state, wanted
from tests.factories import make_series, pin

OVERSEERR = "http://overseerr.lan:5055"


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        save_settings({"overseerr_url": OVERSEERR, "overseerr_api_key": "key",
                       "tmdb_api_key": ""})
        yield c


def recommend(conn, source_id, tmdb_id, title, **kw):
    conn.execute(
        "INSERT INTO recommendations (source_series_id, tmdb_id, title, poster_path, "
        "first_air_date, overview, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (source_id, tmdb_id, title, kw.get("poster_path", "/p.jpg"),
         kw.get("first_air_date", "2019-05-01"), kw.get("overview"), utcnow()),
    )


def seed(conn, user_id):
    """A pin, one recommendation you own, one you do not."""
    silo = make_series(conn, "Silo", tmdb_id=111)
    pin(conn, user_id, silo)
    make_series(conn, "Owned Already", tmdb_id=222)
    recommend(conn, silo, 222, "Owned Already")
    recommend(conn, silo, 333, "Not Yours")
    return silo


# ── What surfaces ──


def test_only_what_you_do_not_own_is_offered(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        rows = wanted(conn, user_id)
    assert [r["title"] for r in rows] == ["Not Yours"]


def test_it_says_which_pin_it_came_from(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        rows = wanted(conn, user_id)
    assert rows[0]["because"] == "Silo"


def test_the_poster_and_year_come_along(db, admin_token):
    """There is no series row to borrow them from, so without these an
    unowned suggestion is a bare string."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        row = wanted(conn, user_id)[0]
    assert row["poster_path"] == "/p.jpg"
    assert row["first_air_date"] == "2019-05-01"


def test_more_pins_pointing_at_it_ranks_it_higher(db, admin_token):
    _, user_id = admin_token
    with session() as conn:
        a = make_series(conn, "Silo", tmdb_id=1)
        b = make_series(conn, "Severance", tmdb_id=2)
        pin(conn, user_id, a)
        pin(conn, user_id, b)
        recommend(conn, a, 900, "Once")
        recommend(conn, a, 901, "Twice")
        recommend(conn, b, 901, "Twice")
        rows = wanted(conn, user_id)
    assert [r["title"] for r in rows] == ["Twice", "Once"]


def test_someone_elses_pins_do_not_suggest_to_you(db, account):
    _, marc = account()
    _, bob = account("bob", "user")
    with session() as conn:
        seed(conn, marc)
        assert wanted(conn, bob) == []


def test_the_section_is_hidden_without_a_key(client, admin_token):
    """A list of things you cannot have is the advert this was filtered out
    to avoid being."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    save_settings({"overseerr_api_key": ""})
    assert "Not in your library" not in client.get("/discover").text


def test_the_section_appears_with_one(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    body = client.get("/discover").text
    assert "Not in your library" in body
    assert "Not Yours" in body
    assert 'id="req-333"' in body


# ── Asking ──


@respx.mock
async def test_requesting_sends_it_to_overseerr(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    route = respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"media": {"status": 3}})
    )
    body = client.post("/api/request/333").json()
    assert body["ok"] is True
    assert body["status"] == "processing"

    sent = route.calls[0].request
    assert sent.headers["X-Api-Key"] == "key"
    import json as _json
    payload = _json.loads(sent.content)
    assert payload["mediaType"] == "tv"
    assert payload["mediaId"] == 333
    assert payload["seasons"] == "all"


@respx.mock
async def test_the_request_is_attributed_to_the_person_who_asked(client, admin_token):
    """The key is one admin credential, so without this everything anyone
    asks for arrives in Overseerr under a single name."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        conn.execute(
            "UPDATE users SET overseerr_user_id = 7 WHERE id = ?", (user_id,)
        )
    route = respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"media": {"status": 2}})
    )
    client.post("/api/request/333")
    import json as _json
    assert _json.loads(route.calls[0].request.content)["userId"] == 7


@respx.mock
async def test_without_a_mapping_no_user_is_claimed(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    route = respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"media": {"status": 2}})
    )
    client.post("/api/request/333")
    import json as _json
    assert "userId" not in _json.loads(route.calls[0].request.content)


@respx.mock
async def test_the_status_is_recorded_immediately(client, admin_token):
    """Waiting for the next sweep would leave the button you just pressed
    looking as though it had not worked."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"media": {"status": 2}})
    )
    client.post("/api/request/333")
    with session() as conn:
        state = media_state(conn, 333)
    assert state["status"] == "pending"
    assert state["requested_by"] == "admin"


@respx.mock
async def test_overseerr_refusing_is_reported(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(500, json={})
    )
    r = client.post("/api/request/333")
    assert r.status_code == 409
    assert "Overseerr said no" in r.json()["detail"]


async def test_requesting_without_a_key_is_refused(client, admin_token):
    save_settings({"overseerr_api_key": ""})
    r = client.post("/api/request/333")
    assert r.status_code == 409
    assert "API key" in r.json()["detail"]


# ── Knowing what has already been asked ──


@respx.mock
async def test_the_sweep_records_what_overseerr_knows(client):
    respx.get(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(200, json={"results": [
            {"media": {"tmdbId": 333, "status": 3},
             "requestedBy": {"displayName": "marc"}},
            {"media": {"tmdbId": 444, "status": 5},
             "requestedBy": {"displayName": "kate"}},
        ]})
    )
    detail = await sync_requests()
    assert "2 request(s) known" in detail
    assert "1 still on their way" in detail

    with session() as conn:
        assert media_state(conn, 333)["status"] == "processing"
        assert media_state(conn, 444)["requested_by"] == "kate"


@respx.mock
async def test_a_cancelled_request_stops_being_reported(client):
    """Replaced wholesale, or something cancelled there lingers here as
    pending for ever."""
    respx.get(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(200, json={"results": [
            {"media": {"tmdbId": 333, "status": 2}},
        ]})
    )
    await sync_requests()
    respx.get(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    await sync_requests()
    with session() as conn:
        assert media_state(conn, 333) is None


@respx.mock
async def test_an_already_requested_show_offers_no_button(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    respx.get(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(200, json={"results": [
            {"media": {"tmdbId": 333, "status": 3},
             "requestedBy": {"displayName": "marc"}},
        ]})
    )
    await sync_requests()

    body = client.get("/discover").text
    assert 'id="req-333"' not in body
    assert "processing" in body


async def test_the_sweep_needs_a_key(client):
    save_settings({"overseerr_api_key": ""})
    assert "API key" in await sync_requests()


# ── The client's own reading of Overseerr ──


@respx.mock
async def test_the_media_status_is_used_not_the_request_status(client):
    """A request can be approved while the show is still downloading, and
    "approved" is not an answer to "can I watch it"."""
    respx.get(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(200, json={"results": [
            {"status": 2, "media": {"tmdbId": 333, "status": 3}},
        ]})
    )
    states = await OverseerrClient().media_states()
    assert states[333].status == "processing"


@respx.mock
async def test_something_that_is_not_an_overseerr_is_called_out(client):
    respx.get(f"{OVERSEERR}/api/v1/status").mock(
        return_value=httpx.Response(200, json={"nope": True})
    )
    body = client.post("/api/settings/test/overseerr").json()
    assert body["ok"] is False
    assert "not like an Overseerr" in body["message"]


@respx.mock
async def test_the_tester_proves_the_key_rather_than_just_the_url(client):
    """/status needs no key, so a green tick from it alone would say nothing
    about whether the key works."""
    respx.get(f"{OVERSEERR}/api/v1/status").mock(
        return_value=httpx.Response(200, json={"version": "1.33.2"})
    )
    users = respx.get(f"{OVERSEERR}/api/v1/user").mock(
        return_value=httpx.Response(200, json={"results": [
            {"id": 1, "displayName": "marc", "email": "marc@example.com"},
        ]})
    )
    body = client.post("/api/settings/test/overseerr").json()
    assert body["ok"] is True
    assert users.call_count == 1
    assert "the key works" in body["message"]


@respx.mock
async def test_without_a_key_the_tester_says_links_only(client):
    save_settings({"overseerr_api_key": ""})
    respx.get(f"{OVERSEERR}/api/v1/status").mock(
        return_value=httpx.Response(200, json={"version": "1.33.2"})
    )
    body = client.post("/api/settings/test/overseerr").json()
    assert body["ok"] is True
    assert "add an api key" in body["message"].lower()


# ── Following a request until it lands ──


def asked_for(conn, tmdb_id, status="processing", who="marc"):
    conn.execute(
        "INSERT INTO overseerr_media (tmdb_id, status, requested_by, updated_at) "
        "VALUES (?, ?, ?, ?)", (tmdb_id, status, who, utcnow()),
    )


def test_requesting_creates_no_series_row(client, admin_token):
    """The thing to be clear about: Overseerr tells Sonarr, Sonarr adds the
    show, and Pinnarr only learns of it when the Sonarr sync next runs."""
    from app.repo import requested

    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        asked_for(conn, 333)
        row = requested(conn, user_id)[0]
    assert row["series_id"] is None
    assert row["remembered_title"] == "Not Yours"
    assert row["status"] == "processing"


def test_once_sonarr_has_it_the_row_appears_and_links_locally(client, admin_token):
    from app.repo import requested

    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        asked_for(conn, 333, status="available")
        # What the Sonarr sweep does when it finally sees it.
        landed = make_series(conn, "Not Yours", tmdb_id=333)
        row = requested(conn, user_id)[0]
    assert row["series_id"] == landed

    body = client.get("/discover").text
    assert f'href="/series/{landed}"' in body
    assert "here — pin it" in body


def test_pinning_it_closes_the_loop(client, admin_token):
    """The section is for requests you have not dealt with. Pinning is
    dealing with it."""
    from app.repo import requested

    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        asked_for(conn, 333)
        landed = make_series(conn, "Not Yours", tmdb_id=333)
        assert len(requested(conn, user_id)) == 1
        pin(conn, user_id, landed)
        assert requested(conn, user_id) == []


def test_media_nobody_asked_for_is_not_listed(client, admin_token):
    """Overseerr knows about everything in the library, most of which was
    imported rather than requested."""
    from app.repo import requested

    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        asked_for(conn, 333, who=None)
        assert requested(conn, user_id) == []


def test_arrived_requests_sort_above_ones_still_coming(client, admin_token):
    from app.repo import requested

    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        asked_for(conn, 333, status="processing")
        asked_for(conn, 444, status="available")
        make_series(conn, "Landed", tmdb_id=444)
        rows = requested(conn, user_id)
    assert [r["tmdb_id"] for r in rows] == [444, 333]


def test_the_asked_for_section_is_hidden_without_a_key(client, admin_token):
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
        asked_for(conn, 333)
    save_settings({"overseerr_api_key": ""})
    assert "Asked for" not in client.get("/discover").text


@respx.mock
async def test_a_request_nudges_the_sonarr_sweep(client, admin_token):
    """Waiting until ten past three to see what you just asked for is the
    difference between a feature and a form."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"media": {"status": 2}})
    )
    client.post("/api/request/333")

    scheduled = {j.id for j in app.state.scheduler.get_jobs()}
    assert "sonarr_series_after_request" in scheduled


@respx.mock
async def test_asking_for_several_things_is_still_one_sweep(client, admin_token):
    """A fixed job id with replace_existing, so six requests in a sitting do
    not become six walks of a two thousand series library."""
    _, user_id = admin_token
    with session() as conn:
        seed(conn, user_id)
    respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"media": {"status": 2}})
    )
    for tmdb_id in (333, 444, 555):
        client.post(f"/api/request/{tmdb_id}")

    sweeps = [j for j in app.state.scheduler.get_jobs()
              if j.id == "sonarr_series_after_request"]
    assert len(sweeps) == 1


# ── The id everything hangs on ──


@respx.mock
async def test_a_sonarr_row_without_a_tmdb_id_gets_one(client, admin_token):
    """Sonarr's metadata is TVDB's and its tmdbId is usually empty, so a show
    it has just added arrives here with no TMDB id — and every join between
    Pinnarr and Overseerr keys on exactly that."""
    from app.jobs.tmdb_sync import resolve_missing_tmdb_ids

    save_settings({"tmdb_api_key": "key"})
    with session() as conn:
        sid = make_series(conn, "Just Added", tvdb_id=555, tmdb_id=None)

    respx.get("https://api.themoviedb.org/3/find/555").mock(
        return_value=httpx.Response(200, json={"tv_results": [{"id": 333}]})
    )
    assert await resolve_missing_tmdb_ids() == 1

    with session() as conn:
        assert conn.execute(
            "SELECT tmdb_id FROM series WHERE id = ?", (sid,)
        ).fetchone()["tmdb_id"] == 333


@respx.mock
async def test_resolving_the_id_is_what_makes_the_card_link_locally(client, admin_token):
    """End to end: without the id the card points at TMDB for ever, however
    long you wait, because nothing can match the series to the request."""
    from app.jobs.tmdb_sync import resolve_missing_tmdb_ids

    _, user_id = admin_token
    save_settings({"tmdb_api_key": "key"})
    with session() as conn:
        seed(conn, user_id)
        asked_for(conn, 333, status="available")
        sid = make_series(conn, "Not Yours", tvdb_id=555, tmdb_id=None)

    assert "here — pin it" not in client.get("/discover").text

    respx.get("https://api.themoviedb.org/3/find/555").mock(
        return_value=httpx.Response(200, json={"tv_results": [{"id": 333}]})
    )
    await resolve_missing_tmdb_ids()

    body = client.get("/discover").text
    assert f'href="/series/{sid}"' in body
    assert "here — pin it" in body


@respx.mock
async def test_a_series_that_already_has_an_id_is_left_alone(client):
    from app.jobs.tmdb_sync import resolve_missing_tmdb_ids

    save_settings({"tmdb_api_key": "key"})
    with session() as conn:
        make_series(conn, "Known", tvdb_id=555, tmdb_id=999)
    assert await resolve_missing_tmdb_ids() == 0


@respx.mock
async def test_tmdb_not_knowing_it_is_not_an_error(client):
    from app.jobs.tmdb_sync import resolve_missing_tmdb_ids

    save_settings({"tmdb_api_key": "key"})
    with session() as conn:
        make_series(conn, "Obscure", tvdb_id=555, tmdb_id=None)
    respx.get("https://api.themoviedb.org/3/find/555").mock(
        return_value=httpx.Response(200, json={"tv_results": []})
    )
    assert await resolve_missing_tmdb_ids() == 0


@respx.mock
async def test_ids_are_resolved_before_the_nightly_budget_bites(client):
    """An unpinned show sorts last in the outlook pass, which is capped — so
    queueing id resolution behind it could leave a new request waiting days."""
    from app.jobs.tmdb_sync import sync_outlook

    save_settings({"tmdb_api_key": "key"})
    with session() as conn:
        make_series(conn, "Just Added", tvdb_id=555, tmdb_id=None)
    respx.get("https://api.themoviedb.org/3/find/555").mock(
        return_value=httpx.Response(200, json={"tv_results": [{"id": 333}]})
    )
    respx.get("https://api.themoviedb.org/3/tv/333").mock(
        return_value=httpx.Response(200, json={"status": "Ended", "in_production": False})
    )
    detail = await sync_outlook()
    assert "1 TMDB id(s) resolved" in detail


# ── A request gets a page of its own ──


TMDB_API = "https://api.themoviedb.org/3"


def summary_response(tmdb_id=333, tvdb_id=777, name="Not Yours"):
    return httpx.Response(200, json={
        "id": tmdb_id,
        "name": name,
        "first_air_date": "2019-05-01",
        "overview": "Something you asked for.",
        "poster_path": "/p.jpg",
        "external_ids": {"tvdb_id": tvdb_id, "imdb_id": "tt123"},
    })


@respx.mock
async def test_requesting_creates_a_page_to_link_to(client, admin_token):
    """A request is a decision, and a decision deserves somewhere to live.
    Without a row there is nothing to link to and nothing to pin."""
    _, user_id = admin_token
    save_settings({"tmdb_api_key": "key"})
    with session() as conn:
        seed(conn, user_id)
    respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"media": {"status": 2}})
    )
    respx.get(f"{TMDB_API}/tv/333").mock(return_value=summary_response())

    body = client.post("/api/request/333").json()
    assert body["series_id"]
    assert body["url"] == f"/series/{body['series_id']}"

    page = client.get(body["url"])
    assert page.status_code == 200
    assert "Not Yours" in page.text


@respx.mock
async def test_the_page_says_it_has_not_arrived_rather_than_looking_broken(
    client, admin_token
):
    _, user_id = admin_token
    save_settings({"tmdb_api_key": "key"})
    with session() as conn:
        seed(conn, user_id)
    respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"media": {"status": 2}})
    )
    respx.get(f"{TMDB_API}/tv/333").mock(return_value=summary_response())

    url = client.post("/api/request/333").json()["url"]
    body = client.get(url).text
    assert "Requested" in body
    assert "pending" in body
    assert "nothing to list" in body


@respx.mock
async def test_the_row_carries_the_tvdb_id_so_sonarr_lands_on_it(client, admin_token):
    """Sonarr matches on tvdb_id before anything else. A row created from
    TMDB without one would not be recognised as the same show, and you would
    end up with two."""
    _, user_id = admin_token
    save_settings({"tmdb_api_key": "key"})
    with session() as conn:
        seed(conn, user_id)
    respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"media": {"status": 2}})
    )
    respx.get(f"{TMDB_API}/tv/333").mock(return_value=summary_response(tvdb_id=777))
    series_id = client.post("/api/request/333").json()["series_id"]

    with session() as conn:
        row = conn.execute(
            "SELECT tvdb_id, tmdb_id, in_sonarr, in_plex FROM series WHERE id = ?",
            (series_id,),
        ).fetchone()
    assert row["tvdb_id"] == 777
    assert row["tmdb_id"] == 333
    assert row["in_sonarr"] == 0


@respx.mock
async def test_when_sonarr_adds_it_the_rows_merge(client, admin_token):
    """The whole reason for fetching the external ids: one show, one row."""
    from app.clients.sonarr import SonarrSeries
    from app.repo import upsert_from_sonarr

    _, user_id = admin_token
    save_settings({"tmdb_api_key": "key"})
    with session() as conn:
        seed(conn, user_id)
    respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"media": {"status": 2}})
    )
    respx.get(f"{TMDB_API}/tv/333").mock(return_value=summary_response(tvdb_id=777))
    series_id = client.post("/api/request/333").json()["series_id"]

    with session() as conn:
        before = conn.execute("SELECT count(*) AS n FROM series").fetchone()["n"]
        # Sonarr, with no tmdbId of its own, as usual.
        landed = upsert_from_sonarr(conn, SonarrSeries(
            sonarr_id=42, tvdb_id=777, tmdb_id=None, imdb_id=None,
            title="Not Yours", sort_title="not yours", year=2019, status="continuing",
            network="BBC", overview="", monitored=True, next_airing=None,
            previous_airing=None, latest_season=1,
        ))
        after = conn.execute("SELECT count(*) AS n FROM series").fetchone()["n"]

    assert landed == series_id
    assert after == before


@respx.mock
async def test_a_show_already_in_the_library_is_not_duplicated(client, admin_token):
    _, user_id = admin_token
    save_settings({"tmdb_api_key": "key"})
    with session() as conn:
        seed(conn, user_id)
        existing = make_series(conn, "Already Here", tvdb_id=777, tmdb_id=None)
    respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"media": {"status": 2}})
    )
    respx.get(f"{TMDB_API}/tv/333").mock(return_value=summary_response(tvdb_id=777))

    assert client.post("/api/request/333").json()["series_id"] == existing
    with session() as conn:
        # And it gained the id it was missing, which is what made it
        # unmatchable to Overseerr in the first place.
        assert conn.execute(
            "SELECT tmdb_id FROM series WHERE id = ?", (existing,)
        ).fetchone()["tmdb_id"] == 333


@respx.mock
async def test_a_requested_show_can_be_pinned_before_it_arrives(client, admin_token):
    """Which is the point of having a row at all."""
    _, user_id = admin_token
    save_settings({"tmdb_api_key": "key"})
    with session() as conn:
        seed(conn, user_id)
    respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"media": {"status": 2}})
    )
    respx.get(f"{TMDB_API}/tv/333").mock(return_value=summary_response())
    series_id = client.post("/api/request/333").json()["series_id"]

    assert client.post(f"/api/series/{series_id}/pin").json()["pinned"] is True


@respx.mock
async def test_tmdb_failing_does_not_lose_the_request(client, admin_token):
    """The request succeeded. Losing the page for it is a smaller failure
    than pretending it never happened."""
    _, user_id = admin_token
    save_settings({"tmdb_api_key": "key"})
    with session() as conn:
        seed(conn, user_id)
    respx.post(f"{OVERSEERR}/api/v1/request").mock(
        return_value=httpx.Response(201, json={"media": {"status": 2}})
    )
    respx.get(f"{TMDB_API}/tv/333").mock(return_value=httpx.Response(500, json={}))

    body = client.post("/api/request/333").json()
    assert body["ok"] is True
    assert body["series_id"] is None
    with session() as conn:
        assert media_state(conn, 333)["status"] == "pending"
