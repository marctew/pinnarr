"""What every route module needs, in one place that imports no routes.

Routes live in app/routes/*. They all need the template environment, and
main.py needs it too for the 403 page — so it cannot live in either without
one importing the other. It lives here, and nothing here imports a route.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import labels
from app.config import get_settings
from app.links import plex_episode

STATIC_DIR = Path(__file__).parent / "static"


def _asset_version() -> str:
    """A cache key that changes when the file does.

    The app version will not do: editing the stylesheet without cutting a
    release would leave every browser on the old copy, which is the
    "force refresh and it is still wrong" failure.
    """
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.glob("*")):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


ASSET_VERSION = _asset_version()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def current_user(request: Request):
    return getattr(request.state, "user", None)


templates.env.globals.update(
    current_user=current_user,
    outlook_label=labels.outlook,
    outlook_badge=labels.outlook_badge,
    status_label=labels.sonarr_status,
    relative_day=labels.relative_day,
    plex_episode=plex_episode,
    version=ASSET_VERSION,
    duration=labels.duration,
    OUTLOOK=labels.OUTLOOK,
    SONARR_STATUS=labels.SONARR_STATUS,
)


def now_local() -> tuple[datetime, ZoneInfo]:
    """An aware "now" and the zone to render it in.

    Deliberately UTC rather than local: everything stored is UTC, and the
    conversion belongs at the point of display, once.
    """
    tz = ZoneInfo(get_settings().tz)
    return datetime.now(UTC), tz
