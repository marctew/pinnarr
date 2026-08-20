"""No rendered link may fall back to the browser's own link colours.

An anchor with `text-decoration: none` and a weight but no `color` inherits
the UA default: blue, and purple once you have clicked it. On the dark theme
that came out pink, and it happened three times in one sitting — the day
panel, Carry on watching and the download queue — because the colour rules
were scoped to `.ep` and each new kind of row quietly missed them.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app import auth, web
from app.db import session
from app.main import app
from tests.factories import iso, make_episode, make_series, pin, watch

CSS = (web.STATIC_DIR / "pinnarr.css").read_text(encoding="utf-8")

#: Anchors whose colour legitimately comes from an ancestor that always
#: encloses them, or which carry no text at all.
#:
#: `art`/`poster`/`thumb` wrap an image. `active` only ever appears on a nav
#: link inside <header>; `nav` only inside a .section heading; `clear` only
#: inside the library rail. Each has exactly one home, so an ancestor rule
#: is not a scoping accident the way `.ep .show` was.
STYLED_ELSEWHERE = {"art", "poster", "thumb", "active", "nav", "clear"}


@pytest.fixture
def client(db, admin_token):
    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        yield c


def colours(css_class: str) -> bool:
    """Does this class carry a colour of its own, wherever it is used?

    Deliberately strict about *where* the rule applies. `.ep .show { color }`
    colours a show name inside an agenda row and nowhere else, so the first
    version of this check — any rule mentioning the class — passed happily
    while the day panel rendered the same class in browser-default purple.
    Only an unscoped rule travels with the class.
    """
    for selectors, body in re.findall(r"([^{}]+)\{([^}]*)\}", CSS):
        if "color:" not in body:
            continue
        for selector in selectors.split(","):
            # `.show`, `.show:hover`, `.pill.watched` — but not `.ep .show`,
            # which only applies inside something else.
            if re.fullmatch(
                rf"\s*\.{re.escape(css_class)}(?:[.:][\w-]+(?:\([^)]*\))?)*\s*",
                selector,
            ):
                return True
    return False


def bare_links(body: str) -> list[str]:
    """Anchors that name a class of their own but never get a colour from it.

    Class-less anchors are left alone: those are prose and nav links, styled
    by an ancestor rule on purpose. The failure this catches is the other
    kind — a link given its own class for layout and typography, where the
    colour was simply forgotten.
    """
    out = []
    for tag in re.findall(r"<a\b[^>]*>", body):
        if 'aria-hidden="true"' in tag:
            continue  # An image wrapper has no text to colour.
        found = re.search(r'class="([^"]*)"', tag)
        names = {n for n in (found.group(1).split() if found else []) if n}
        if not names or names & STYLED_ELSEWHERE:
            continue
        if not any(colours(n) for n in names):
            out.append(tag)
    return out


@pytest.fixture
def populated(db, admin_token):
    """One of everything a page might list."""
    _, user_id = admin_token
    with session() as conn:
        sid = make_series(conn, "Silo", sonarr_id=7, plex_rating_key="99",
                          outlook="dated", next_airing=iso(days=3))
        pin(conn, user_id, sid)
        for number, offset, has in ((1, -3, 1), (2, 1, 0), (3, 4, 0)):
            eid = make_episode(conn, sid, season=1, episode=number, runtime=45,
                               air_date_utc=iso(days=offset), has_file=has,
                               in_plex=has, arrived_at=iso(days=offset) if has else None)
            if number == 1:
                watch(conn, user_id, eid)
        conn.execute(
            "INSERT INTO download_queue (sonarr_episode_id, status, percent, "
            "first_seen_at, progress_at, updated_at) VALUES (7, 'downloading', 40, "
            "?, ?, ?)", (iso(hours=-2), iso(hours=-1), iso()),
        )
        conn.execute(
            "UPDATE episodes SET sonarr_episode_id = 7 WHERE season = 1 AND episode = 2"
        )
    return user_id


@pytest.mark.parametrize(
    "path", ["/", "/ready", "/library", "/gaps", "/retire", "/discover", "/downloads"]
)
def test_no_link_uses_the_browsers_default_colours(client, populated, path):
    bare = bare_links(client.get(path).text)
    assert not bare, f"{path}: {bare}"


def test_the_check_would_have_caught_it(client, populated):
    """A link with layout but no colour is exactly what shipped."""
    assert bare_links('<a class="totally-unstyled" href="/x">Silo</a>')


def test_a_series_name_is_body_coloured_not_link_coloured(client, populated):
    """The specific symptom: anything you had already clicked went purple."""
    assert ".show { color: var(--ink)" in CSS


def test_the_pill_colours_are_not_scoped_to_one_row_type(client, populated):
    """Scoping them to .ep is what left the day panel and the queue with
    unstyled pills in the first place."""
    assert ".state-available .pill" in CSS
    assert ".ep.state-available .pill" not in CSS
