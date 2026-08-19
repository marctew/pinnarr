"""SQLite access and migrations.

Deliberately stdlib sqlite3 rather than an ORM. The whole schema is two
real tables plus lookups, the queries are the interesting part, and it
keeps the image small.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> str:
    """ISO 8601 UTC, second precision. Every timestamp we store uses this."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    settings = get_settings()
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    # WAL so the hourly availability job doesn't block page loads.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """Transactional connection. Commits on clean exit, rolls back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate() -> None:
    """Apply any .sql files in migrations/ that haven't run yet, in filename order."""
    with session() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = {r["filename"] for r in conn.execute("SELECT filename FROM schema_migrations")}

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            log.info("applying migration %s", path.name)
            conn.executescript(path.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                (path.name, utcnow()),
            )


# ── sync_log helpers ─────────────────────────────


def job_started(job: str) -> int:
    with session() as conn:
        cur = conn.execute(
            "INSERT INTO sync_log (job, started_at) VALUES (?, ?)", (job, utcnow())
        )
        return int(cur.lastrowid or 0)


def job_finished(run_id: int, status: str, detail: str = "") -> None:
    with session() as conn:
        conn.execute(
            "UPDATE sync_log SET finished_at = ?, status = ?, detail = ? WHERE id = ?",
            (utcnow(), status, detail[:2000], run_id),
        )


def last_runs() -> list[sqlite3.Row]:
    """Most recent run per job, for /healthz."""
    with session() as conn:
        return list(
            conn.execute(
                """
                SELECT job, started_at, finished_at, status, detail
                FROM sync_log
                WHERE id IN (SELECT MAX(id) FROM sync_log GROUP BY job)
                ORDER BY job
                """
            )
        )


# ── settings table (runtime-tunable values) ──────


def get_setting(key: str, default: str | None = None) -> str | None:
    with session() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with session() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
