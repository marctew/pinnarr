"""Deep links out to the tools that own the data.

Everything here degrades to None rather than producing a broken URL: a
missing link is invisible, a link that 404s is a bug report.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.config import get_settings
from app.db import get_setting


def plex(row: Any) -> str | None:
    """Deep link into the Plex web app for this show.

    Needs the server's machineIdentifier, which the plex_library job caches;
    before the first sync there is no link to give.
    """
    key = row["plex_rating_key"]
    machine = get_setting("plex_machine_id")
    base = get_settings().plex_url
    if not (key and machine and base):
        return None
    path = quote(f"/library/metadata/{key}", safe="")
    return f"{base}/web/index.html#!/server/{machine}/details?key={path}"


def plex_episode(rating_key: str | None) -> str | None:
    """Deep link to one episode, given Plex's own id for it."""
    machine = get_setting("plex_machine_id")
    base = get_settings().plex_url
    if not (rating_key and machine and base):
        return None
    path = quote(f"/library/metadata/{rating_key}", safe="")
    return f"{base}/web/index.html#!/server/{machine}/details?key={path}"


def overseerr_person(tmdb_person_id: int | None) -> str | None:
    """Overseerr's page for one person.

    Overseerr routes people by TMDB id, which is the same id Pinnarr already
    stores against a credit — so this is an exact link rather than a search
    that hopes for the best.
    """
    base = get_settings().overseerr_url
    if not (base and tmdb_person_id):
        return None
    return f"{base}/person/{int(tmdb_person_id)}"


def overseerr_search(term: str | None) -> str | None:
    """Overseerr's search, for when a name is all there is."""
    base = get_settings().overseerr_url
    if not (base and term):
        return None
    return f"{base}/search?query={quote(term)}"


def sonarr(row: Any) -> str | None:
    """Sonarr routes its series pages by slug, not by id."""
    base = get_settings().sonarr_url
    slug = row["title_slug"]
    if not (base and slug):
        return None
    return f"{base}/series/{slug}"


def externals(row: Any) -> list[dict[str, str]]:
    """Every off-site link we can build for a series, in a fixed order."""
    out: list[dict[str, str]] = []
    if url := plex(row):
        out.append({"name": "Plex", "url": url})
    if url := sonarr(row):
        out.append({"name": "Sonarr", "url": url})
    if row["tvdb_id"]:
        out.append({"name": "TVDB", "url": f"https://thetvdb.com/dereferrer/series/{row['tvdb_id']}"})
    if row["tmdb_id"]:
        out.append({"name": "TMDB", "url": f"https://www.themoviedb.org/tv/{row['tmdb_id']}"})
    if row["imdb_id"]:
        out.append({"name": "IMDb", "url": f"https://www.imdb.com/title/{row['imdb_id']}/"})
    return out


def missing_links(row: Any) -> list[str]:
    """Why a link you might expect isn't there.

    A button that is simply absent is indistinguishable from a feature that
    does not exist, which is exactly how this looked the first time it failed.
    """
    settings = get_settings()
    reasons: list[str] = []

    if settings.plex_url and row["plex_rating_key"] and not get_setting("plex_machine_id"):
        reasons.append(
            "The Plex link needs the server's id, which is read during a sync — "
            "hit Test Plex in Settings, or run the plex_library job."
        )
    if settings.sonarr_url and row["sonarr_id"] and not row["title_slug"]:
        reasons.append(
            "The Sonarr link needs the series slug, which arrives with the "
            "sonarr_series job."
        )
    return reasons
