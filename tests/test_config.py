"""Settings parsing, with an eye on the .env path specifically.

These load through a real .env file rather than os.environ, because the bug
they pin lived in the dotenv source: pydantic-settings JSON-decodes complex
field types there, ahead of any validator.
"""

from __future__ import annotations

import pytest

from app.config import Settings


def settings_from_env_file(tmp_path, body: str) -> Settings:
    env = tmp_path / ".env"
    env.write_text(body, encoding="utf-8")
    return Settings(_env_file=env)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),           # the .env.example default — must not raise
        ("2", [2]),
        ("2,5", [2, 5]),
        ("2, 5", [2, 5]),
        ("2,5,", [2, 5]),   # stray trailing comma
    ],
)
def test_plex_tv_sections_parses_from_env_file(tmp_path, raw, expected):
    s = settings_from_env_file(tmp_path, f"PLEX_TV_SECTIONS={raw}\n")
    assert s.plex_tv_sections == expected


def test_plex_tv_sections_absent_defaults_to_empty(tmp_path):
    s = settings_from_env_file(tmp_path, "PLEX_URL=http://x:32400\n")
    assert s.plex_tv_sections == []


def test_urls_lose_their_trailing_slash(tmp_path):
    s = settings_from_env_file(tmp_path, "SONARR_URL=http://x:8989/\n")
    assert s.sonarr_url == "http://x:8989"
