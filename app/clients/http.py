"""Shared HTTP behaviour for every upstream client.

All four integrations are on the LAN or a well-behaved public API, so the
policy is simple: short timeouts, retry a couple of times on transient
failures, and raise a typed error the jobs can log without caring which
service it came from.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
RETRY_STATUS = {429, 502, 503, 504}
MAX_ATTEMPTS = 3


class UpstreamError(RuntimeError):
    """Any failure talking to Plex/Sonarr/Tautulli/TMDB/ntfy."""

    def __init__(self, service: str, message: str) -> None:
        super().__init__(f"{service}: {message}")
        self.service = service


async def request_json(
    service: str,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
    timeout: httpx.Timeout = DEFAULT_TIMEOUT,
) -> Any:
    """Make a request and return decoded JSON, retrying transient failures."""
    hdrs = {"Accept": "application/json", **(headers or {})}
    last: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.request(
                    method, url, headers=hdrs, params=params, json=json_body
                )

            if resp.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                # Honour Retry-After when the server bothers to send one.
                delay = float(resp.headers.get("Retry-After", 2**attempt))
                log.warning(
                    "%s %s → %s, retrying in %.1fs", service, url, resp.status_code, delay
                )
                await asyncio.sleep(min(delay, 30.0))
                continue

            if resp.status_code == 401 or resp.status_code == 403:
                raise UpstreamError(service, f"authentication rejected ({resp.status_code}) — check the API key")
            resp.raise_for_status()

            if not resp.content:
                return None
            return resp.json()

        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(2**attempt)
                continue
        except httpx.HTTPStatusError as exc:
            raise UpstreamError(service, f"HTTP {exc.response.status_code} from {url}") from exc
        except ValueError as exc:  # JSON decode
            raise UpstreamError(service, f"invalid JSON from {url}") from exc

    raise UpstreamError(service, f"unreachable after {MAX_ATTEMPTS} attempts: {last}")
