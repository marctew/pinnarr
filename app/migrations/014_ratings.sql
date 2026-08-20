-- TMDB carries a vote average per episode. Stored so the shape of a season
-- can be drawn without a network call per page view.
ALTER TABLE episodes ADD COLUMN rating REAL;

-- Taste-driven suggestions, as opposed to Discover's date-driven ones.
-- Refreshed nightly for pinned series only: a dozen calls, not two thousand.
CREATE TABLE IF NOT EXISTS recommendations (
    source_series_id  INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    tmdb_id           INTEGER NOT NULL,
    title             TEXT,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (source_series_id, tmdb_id)
);
