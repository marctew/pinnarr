"""Configuration, in two layers.

The split is forced rather than stylistic. Bootstrap has to come from the
environment, because you cannot read the database's location out of the
database. Everything else lives in the `settings` table and is edited in the
admin panel at /settings — the environment is not consulted for it.

Consequence worth knowing: editing .env no longer changes any integration.
The panel is the only way in.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Final

from pydantic import BaseModel, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class Bootstrap(BaseSettings):
    """Read once from the environment, before the database is reachable."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_path: str = "/data/pinnarr.db"
    log_level: str = "INFO"


@lru_cache
def get_bootstrap() -> Bootstrap:
    return Bootstrap()


class Settings(BaseModel):
    """Everything the admin panel owns. Values arrive as strings from SQLite
    and are coerced here, so the panel never has to care about types."""

    # ── Pinnarr ──────────────────────────────────
    pinnarr_base_url: str = "http://localhost:8737"
    tz: str = "Europe/London"
    webhook_secret: str = ""

    # ── Plex ─────────────────────────────────────
    plex_url: str = ""
    plex_token: str = ""
    plex_tv_sections: list[int] = []

    # ── Sonarr ───────────────────────────────────
    sonarr_url: str = ""
    sonarr_api_key: str = ""

    #: Mirror pins as Sonarr tags, and pins back from them. Off by default:
    #: it is the second place Pinnarr writes to another service.
    sonarr_tag_sync: bool = False

    # ── Tautulli ─────────────────────────────────
    tautulli_url: str = ""
    tautulli_api_key: str = ""

    # ── TMDB ─────────────────────────────────────
    tmdb_api_key: str = ""

    # ── Radarr (v1.5) ────────────────────────────
    radarr_enabled: bool = False
    radarr_url: str = ""
    radarr_api_key: str = ""

    # ── Notifications ────────────────────────────
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""
    notify_on_arrival: bool = True
    #: Hold arrivals briefly and send one push per series instead of one per
    #: episode. A season pack otherwise buzzes ten times for a single event.
    #: 0 pushes immediately from the webhook, which is instant and noisier.
    notify_batch_minutes: int = 5
    digest_enabled: bool = True
    digest_cron: str = "0 8 * * 1"

    # ── Calendar ─────────────────────────────────
    #: Sonarr keeps rows for episodes it is not chasing — specials you never
    #: wanted, seasons you skipped. They are real broadcasts, so they are
    #: stored either way; this decides whether they clutter the calendar.
    show_unmonitored: bool = False
    #: Season 0. Sonarr does not reliably mark specials unmonitored even when
    #: the season is, so hiding them cannot rely on that flag — and "aired,
    #: not arrived" for a Christmas one-off you never wanted is pure noise.
    show_specials: bool = False

    # ── Outlook thresholds (SPEC §10) ────────────
    hiatus_months: int = 9
    dormant_months: int = 18

    @field_validator("plex_tv_sections", mode="before")
    @classmethod
    def _split_sections(cls, v: object) -> object:
        """Accept "2,5" or "2, 5" from the form; empty means auto-detect."""
        if isinstance(v, str):
            return [int(p) for p in v.replace(" ", "").split(",") if p]
        return v

    @field_validator(
        "plex_url", "sonarr_url", "tautulli_url", "radarr_url", "ntfy_url",
        mode="before",
    )
    @classmethod
    def _strip_trailing_slash(cls, v: object) -> object:
        return v.rstrip("/") if isinstance(v, str) else v

    # ── Readiness ────────────────────────────────

    @property
    def plex_configured(self) -> bool:
        return bool(self.plex_url and self.plex_token)

    @property
    def sonarr_configured(self) -> bool:
        return bool(self.sonarr_url and self.sonarr_api_key)

    @property
    def tautulli_configured(self) -> bool:
        return bool(self.tautulli_url and self.tautulli_api_key)

    @property
    def tmdb_configured(self) -> bool:
        return bool(self.tmdb_api_key)

    @property
    def ntfy_configured(self) -> bool:
        return bool(self.ntfy_url and self.ntfy_topic)

    def missing_config(self) -> list[str]:
        """Human-readable list of what still needs setting up."""
        missing = []
        if not self.plex_configured:
            missing.append("Plex (URL and token)")
        if not self.sonarr_configured:
            missing.append("Sonarr (URL and API key)")
        if not self.tautulli_configured:
            missing.append("Tautulli (URL and API key) — optional")
        if not self.tmdb_configured:
            missing.append("TMDB (API key) — needed for season outlook")
        if not self.ntfy_configured:
            missing.append("ntfy (topic) — optional")
        if not self.webhook_secret:
            missing.append("Webhook secret — the Sonarr webhook is disabled without it")
        return missing


#: Fields never sent to the browser, and only overwritten when the form
#: submits a non-empty value. An empty box means "leave it alone", not "clear
#: it" — otherwise every save with an untouched password field wipes the key.
SECRET_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "plex_token",
        "sonarr_api_key",
        "tautulli_api_key",
        "tmdb_api_key",
        "ntfy_token",
        "radarr_api_key",
        "webhook_secret",
    }
)

#: Fields the scheduler reads when it is built, so changing one has to
#: rebuild it — otherwise the new cron sits in the database doing nothing.
SCHEDULING_FIELDS: Final[frozenset[str]] = frozenset(
    {"tz", "digest_enabled", "digest_cron"}
)


@lru_cache
def get_settings() -> Settings:
    """The live settings, cached. Invalidated by save_settings()."""
    from app.db import all_settings

    stored = all_settings()
    known = {k: v for k, v in stored.items() if k in Settings.model_fields}
    try:
        return Settings(**known)
    except ValidationError as exc:
        # A bad row must not make the app unbootable — that would leave you
        # with no way to reach the panel and fix it.
        log.error("stored settings failed validation, falling back to defaults: %s", exc)
        return Settings()


def save_settings(values: dict[str, Any]) -> Settings:
    """Persist a partial update and return the settings as they now stand.

    Validates the merged result before writing, so a bad value is rejected
    whole rather than leaving half a form applied.
    """
    from app.db import set_setting

    current = get_settings()
    merged = current.model_dump()
    merged.update(values)
    validated = Settings(**merged)

    for key in values:
        if key not in Settings.model_fields:
            continue
        stored = getattr(validated, key)
        if isinstance(stored, list):
            stored = ",".join(str(x) for x in stored)
        elif isinstance(stored, bool):
            stored = "true" if stored else "false"
        set_setting(key, str(stored))

    get_settings.cache_clear()
    return get_settings()
