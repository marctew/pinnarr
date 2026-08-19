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

    from app.config import get_settings

    get_settings.cache_clear()

    from app.db import migrate, session

    migrate()
    try:
        yield session
    finally:
        get_settings.cache_clear()
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.unlink(path + suffix)
