"""The HTTP surface, driven through a real lifespan.

TestClient as a context manager runs startup/shutdown, which is the point:
these tests exist because build_scheduler() was fully written but never
called, so the app booted clean and silently synced nothing.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.jobs import REGISTRY, build_scheduler
from app.main import app

EXPECTED_JOBS = {
    "plex_library",
    "sonarr_series",
    "sonarr_calendar",
    "tautulli_history",
    "tmdb_status",
    "plex_availability",
    "reconcile",
}


@pytest.fixture
def client(db):
    with TestClient(app) as c:
        yield c


def test_scheduler_runs_and_has_every_spec_job(client):
    body = client.get("/healthz").json()
    assert body["scheduler"]["running"] is True
    assert {j["id"] for j in body["scheduler"]["jobs"]} >= EXPECTED_JOBS


def test_every_scheduled_job_has_a_next_run(client):
    body = client.get("/healthz").json()
    assert all(j["next_run"] for j in body["scheduler"]["jobs"])


def test_health_is_degraded_when_the_scheduler_is_not_running(db, monkeypatch):
    # No lifespan, so app.state carries no scheduler and nothing will ever
    # sync. A clean sync_log must not read as healthy in that state.
    monkeypatch.delattr(app.state, "scheduler", raising=False)
    body = TestClient(app).get("/healthz").json()
    assert body["scheduler"]["running"] is False
    assert body["scheduler"]["jobs"] == []
    assert body["status"] == "degraded"


def test_build_scheduler_populates_the_registry(db):
    build_scheduler()
    assert set(REGISTRY) >= EXPECTED_JOBS


def test_unknown_job_is_a_404_that_lists_the_real_ones(client):
    r = client.post("/api/sync/nope")
    assert r.status_code == 404
    assert "plex_library" in r.json()["detail"]


def test_trigger_runs_the_job_and_reports_sync_log_status(client):
    r = client.post("/api/sync/plex_library")
    assert r.status_code == 200
    body = r.json()
    assert body["job"] == "plex_library"
    # Unconfigured, so the job short-circuits — but it must still record a run.
    assert body["status"] == "ok"
    assert "not configured" in body["detail"]


def test_a_triggered_run_shows_up_on_healthz(client):
    client.post("/api/sync/sonarr_series")
    runs = {r["job"]: r for r in client.get("/healthz").json()["last_runs"]}
    assert runs["sonarr_series"]["status"] == "ok"
    assert runs["sonarr_series"]["finished_at"]


def test_a_completely_fresh_database_boots(unmigrated_db):
    """First start on a new install: nothing exists, not even the schema."""
    assert not unmigrated_db.exists()
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
    assert unmigrated_db.exists()
