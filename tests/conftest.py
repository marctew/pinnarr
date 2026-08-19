import contextlib
import os
import tempfile

import pytest


@pytest.fixture
def db(monkeypatch):
    """A migrated, empty database in a temp file, isolated per test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    monkeypatch.setenv("DATABASE_PATH", path)

    from app.config import get_bootstrap, get_settings

    # Both caches: bootstrap holds the path we just changed, and get_settings
    # holds values read out of whichever database was current before.
    get_bootstrap.cache_clear()
    get_settings.cache_clear()

    from app.db import migrate, session

    migrate()
    try:
        yield session
    finally:
        get_bootstrap.cache_clear()
        get_settings.cache_clear()
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.unlink(path + suffix)


@pytest.fixture
def unmigrated_db(monkeypatch, tmp_path):
    """A database path that does not exist yet — a genuinely fresh install.

    The `db` fixture migrates before handing over, which is what let a
    startup-ordering bug through: settings were read before the table
    holding them was created.
    """
    path = tmp_path / "fresh.db"

    monkeypatch.setenv("DATABASE_PATH", str(path))

    from app.config import get_bootstrap, get_settings

    get_bootstrap.cache_clear()
    get_settings.cache_clear()
    try:
        yield path
    finally:
        get_bootstrap.cache_clear()
        get_settings.cache_clear()
