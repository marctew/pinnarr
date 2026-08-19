"""Configuration, loaded from the environment.

Every integration is optional at startup. Pinnarr boots with nothing
configured and tells you what's missing on the health page, rather than
crash-looping and leaving you reading container logs to find a typo.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── Pinnarr ──────────────────────────────────
    pinnarr_base_url: str = "http://localhost:8737"
    tz: str = "Europe/London"
    database_path: str = "/data/pinnarr.db"
    log_level: str = "INFO"
    webhook_secret: str = ""

    # ── Plex ─────────────────────────────────────
    plex_url: str = ""
    plex_token: str = ""
    # NoDecode is load-bearing: pydantic-settings JSON-decodes complex
    # field types straight out of .env, so "2,5" — and even an empty
    # value — raises before the validator below ever runs.
    plex_tv_sections: Annotated[list[int], NoDecode] = []

    # ── Sonarr ───────────────────────────────────
    sonarr_url: str = ""
    sonarr_api_key: str = ""

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
    digest_enabled: bool = True
    digest_cron: str = "0 8 * * 1"

    # ── Outlook thresholds (SPEC §10) ────────────
    hiatus_months: int = 9
    dormant_months: int = 18

    @field_validator("plex_tv_sections", mode="before")
    @classmethod
    def _split_sections(cls, v: object) -> object:
        """Accept "2,5" or "2, 5" from the environment; empty means auto-detect."""
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
            missing.append("Plex (PLEX_URL, PLEX_TOKEN)")
        if not self.sonarr_configured:
            missing.append("Sonarr (SONARR_URL, SONARR_API_KEY)")
        if not self.tautulli_configured:
            missing.append("Tautulli (TAUTULLI_URL, TAUTULLI_API_KEY) — optional")
        if not self.tmdb_configured:
            missing.append("TMDB (TMDB_API_KEY) — needed for season outlook")
        if not self.ntfy_configured:
            missing.append("ntfy (NTFY_TOPIC) — optional")
        if not self.webhook_secret:
            missing.append("WEBHOOK_SECRET — the Sonarr webhook is disabled without it")
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
