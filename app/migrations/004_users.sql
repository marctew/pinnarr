-- Multi-user. Pins move out of the series row and become per-user.
--
-- series.pinned stays, but its meaning narrows to "pinned by at least one
-- user". It is denormalised from `pins` and maintained on every pin change.
-- The sync jobs want exactly that question — the calendar and availability
-- fetches should cover anything anyone follows — so they need no changes.

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY,
    username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash  TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'user',   -- admin | user
    ntfy_topic     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pins (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    series_id  INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    pinned_at  TEXT NOT NULL,
    pin_batch  TEXT,
    notify     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, series_id)
);
CREATE INDEX IF NOT EXISTS idx_pins_series ON pins (series_id);
CREATE INDEX IF NOT EXISTS idx_pins_batch  ON pins (user_id, pin_batch);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

-- Dedupe has to be per user now: one person's push must not suppress
-- another's for the same episode.
CREATE TABLE IF NOT EXISTS episode_notifications (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    episode_id   INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    notified_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, episode_id)
);
