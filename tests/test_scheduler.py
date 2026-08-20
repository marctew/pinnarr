"""The scheduler and the job registry are two lists that have to agree.

`@tracked("name")` populates the registry; `add_job(id="name")` schedules it.
They are separate string literals in separate files, and nothing connects
them. When they drift, /settings/jobs shows a job with a blank next-run and
/healthz shows a scheduled id with no history — neither of which looks like
an error, which is what makes it worth a test.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import auth
from app.jobs import REGISTRY, build_scheduler
from app.main import app


def scheduled_ids(db) -> set[str]:
    return {job.id for job in build_scheduler().get_jobs()}


def test_every_registered_job_is_scheduled(db):
    """A job nobody runs is a job that silently never happens."""
    assert not set(REGISTRY) - scheduled_ids(db)


def test_every_scheduled_job_is_registered(db):
    """A scheduled id with no @tracked wrapper writes nothing to sync_log, so
    it cannot be run by hand and its failures are invisible."""
    assert not scheduled_ids(db) - set(REGISTRY)


def test_the_digest_only_schedules_when_enabled(db):
    from app.config import save_settings

    save_settings({"digest_enabled": "false"})
    assert "weekly_digest" not in scheduled_ids(db)
    save_settings({"digest_enabled": "true"})
    assert "weekly_digest" in scheduled_ids(db)


def test_a_broken_cron_disables_the_digest_rather_than_the_app(db):
    """A bad expression in the panel must not stop the other nineteen jobs."""
    from app.config import save_settings

    save_settings({"digest_enabled": "true", "digest_cron": "not a cron"})
    ids = scheduled_ids(db)
    assert "weekly_digest" not in ids
    assert "sonarr_series" in ids


# ── Static assets ──


def test_the_stylesheet_serves_without_a_session(db):
    """It has to load on the login page, where by definition nobody is
    signed in. Sending it through the auth gate gives an unstyled form."""
    with TestClient(app) as c:
        r = c.get("/static/pinnarr.css")
    assert r.status_code == 200
    assert ":root" in r.text


def test_the_login_page_links_the_stylesheet(db):
    with TestClient(app) as c:
        body = c.get("/login").text
    assert "/static/pinnarr.css" in body


def test_the_cache_key_follows_the_file_not_the_release(db, admin_token):
    """A version-pinned key would leave every browser on the old stylesheet
    after an edit, which is the "force refresh and it's still wrong" bug."""
    from app import main

    token, _ = admin_token
    with TestClient(app) as c:
        c.cookies.set(auth.COOKIE, token)
        body = c.get("/library").text
    assert f"pinnarr.css?v={main.ASSET_VERSION}" in body

    css = main.STATIC_DIR / "pinnarr.css"
    before = css.read_bytes()
    try:
        css.write_bytes(before + b"/* edited */")
        assert main._asset_version() != main.ASSET_VERSION
    finally:
        css.write_bytes(before)
    assert main._asset_version() == main.ASSET_VERSION
