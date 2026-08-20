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


@pytest.fixture
def account(db):
    """An admin account plus a live session token.

    Returns a factory, so a test that cares about roles can make a standard
    user as well and check one cannot reach the other's pins.
    """
    from app import auth
    from app.db import session

    def make(username: str = "admin", role: str = "admin") -> tuple[str, int]:
        with session() as conn:
            user_id = auth.create_user(conn, username, "password123", role)
            token = auth.start_session(conn, user_id)
        return token, user_id

    return make


@pytest.fixture
def admin_token(account):
    return account()


@pytest.fixture
def pushes(monkeypatch):
    """Capture what would have gone to ntfy.

    Patched at the client, not at app.notify, so the logging layer above it
    runs for real — a history that only works in production is not a history.
    """
    sent: list[dict] = []

    async def fake_send(title, message, *, tags="tv", priority="default",
                        click=None, topic=None):
        sent.append({"title": title, "message": message, "topic": topic})
        return True

    from app import notify

    monkeypatch.setattr(notify.ntfy, "send", fake_send)
    return sent
