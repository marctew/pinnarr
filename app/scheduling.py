"""Starting and restarting the job scheduler.

Its own module because two places need it and neither should import the
other: main.py builds the scheduler at startup, and the settings form
rebuilds it whenever a change would otherwise sit in the database doing
nothing.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.jobs import build_scheduler


def stop_scheduler(app: FastAPI) -> None:
    """Stop the current scheduler if there is one and it is running.

    wait=False: a sync mid-flight shouldn't hold up shutdown. Anything it
    half-wrote is picked up by the next run, since the jobs are upserts.
    """
    current = getattr(app.state, "scheduler", None)
    if current is not None and current.running:
        current.shutdown(wait=False)


def restart_scheduler(app: FastAPI) -> None:
    """Rebuild the schedule against the settings as they now are.

    Cron triggers bake in the timezone and expression when the job is added,
    so a saved change is inert until the scheduler is built again.
    """
    stop_scheduler(app)
    scheduler = build_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
