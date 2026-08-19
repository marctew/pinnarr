"""ntfy publisher.

Plain HTTP POST, no SDK. Behind a narrow send() so swapping to Pushover,
Gotify or a Discord webhook later is a new module plus a config line, not
a refactor of the notification jobs.
"""

from __future__ import annotations

import logging

import httpx

from app.clients.http import DEFAULT_TIMEOUT
from app.config import get_settings

log = logging.getLogger(__name__)


async def send(
    title: str,
    message: str,
    *,
    tags: str = "tv",
    priority: str = "default",
    click: str | None = None,
    topic: str | None = None,
) -> bool:
    """Publish one notification. Returns False on failure — never raises.

    A notification that fails must not roll back the sync that triggered it;
    the episode is still in Plex whether or not the phone buzzed.
    """
    s = get_settings()
    # The server and any token are shared configuration; the topic is per
    # user, so callers pass it in.
    target = topic or s.ntfy_topic
    if not (s.ntfy_url and target):
        log.debug("ntfy not configured, dropping notification: %s", title)
        return False

    headers = {
        "Title": title,
        "Tags": tags,
        "Priority": priority,
        "Click": click or s.pinnarr_base_url,
    }
    if s.ntfy_token:
        headers["Authorization"] = f"Bearer {s.ntfy_token}"

    url = f"{s.ntfy_url}/{target}"
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
            resp = await client.post(url, content=message.encode("utf-8"), headers=headers)
        if resp.status_code >= 400:
            log.warning("ntfy rejected notification: HTTP %s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except httpx.HTTPError as exc:
        log.warning("ntfy unreachable: %s", exc)
        return False
