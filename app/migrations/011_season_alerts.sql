-- One nudge per show per user. Without this the weekly job would re-announce
-- the same three shows every Monday until you pinned them, which is how a
-- helpful notification becomes one you swipe away without reading.
CREATE TABLE IF NOT EXISTS season_alerts (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    series_id   INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    next_airing TEXT,
    notified_at TEXT NOT NULL,
    PRIMARY KEY (user_id, series_id)
);
