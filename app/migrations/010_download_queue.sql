-- What Sonarr is downloading, refreshed every few minutes. Without it, an
-- episode that aired and has not appeared looks identical whether a download
-- is 80% done or nothing has been found at all — and only the second is a
-- problem worth showing in red.
CREATE TABLE IF NOT EXISTS download_queue (
    sonarr_episode_id  INTEGER PRIMARY KEY,
    status             TEXT,
    percent            REAL NOT NULL DEFAULT 0,
    time_left          TEXT,
    message            TEXT,
    updated_at         TEXT NOT NULL
);
