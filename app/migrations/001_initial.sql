-- Pinnarr initial schema. See SPEC.md §7.

CREATE TABLE IF NOT EXISTS series (
    id                   INTEGER PRIMARY KEY,
    tvdb_id              INTEGER UNIQUE,
    tmdb_id              INTEGER,
    imdb_id              TEXT,
    plex_rating_key      TEXT,
    plex_section_id      INTEGER,
    sonarr_id            INTEGER,

    title                TEXT NOT NULL,
    sort_title           TEXT,
    year                 INTEGER,
    network              TEXT,
    poster_url           TEXT,
    overview             TEXT,

    -- status as reported by each source
    sonarr_status        TEXT,
    tmdb_status          TEXT,
    in_production        INTEGER,

    -- season tracking, feeds the outlook ladder
    next_airing          TEXT,
    previous_airing      TEXT,
    latest_season        INTEGER,
    latest_aired_season  INTEGER,
    outlook              TEXT,
    outlook_computed_at  TEXT,

    in_plex              INTEGER NOT NULL DEFAULT 0,
    in_sonarr            INTEGER NOT NULL DEFAULT 0,
    match_confidence     TEXT NOT NULL DEFAULT 'exact',

    pinned               INTEGER NOT NULL DEFAULT 0,
    pinned_at            TEXT,
    pin_batch            TEXT,
    notify               INTEGER NOT NULL DEFAULT 1,

    last_watched_at      TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    id                 INTEGER PRIMARY KEY,
    series_id          INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    sonarr_episode_id  INTEGER UNIQUE,
    season             INTEGER NOT NULL,
    episode            INTEGER NOT NULL,
    title              TEXT,
    air_date_utc       TEXT,
    runtime            INTEGER,
    monitored          INTEGER NOT NULL DEFAULT 1,
    has_file           INTEGER NOT NULL DEFAULT 0,
    in_plex            INTEGER NOT NULL DEFAULT 0,
    arrived_at         TEXT,
    notified_at        TEXT,
    updated_at         TEXT NOT NULL,
    UNIQUE (series_id, season, episode)
);

CREATE TABLE IF NOT EXISTS genres (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS series_genres (
    series_id INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    genre_id  INTEGER NOT NULL REFERENCES genres(id)  ON DELETE CASCADE,
    PRIMARY KEY (series_id, genre_id)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_episodes_air     ON episodes (air_date_utc);
CREATE INDEX IF NOT EXISTS idx_episodes_series  ON episodes (series_id);
CREATE INDEX IF NOT EXISTS idx_series_pinned    ON series (pinned);
CREATE INDEX IF NOT EXISTS idx_series_outlook   ON series (outlook);
CREATE INDEX IF NOT EXISTS idx_series_section   ON series (plex_section_id);
CREATE INDEX IF NOT EXISTS idx_series_tvdb      ON series (tvdb_id);
CREATE INDEX IF NOT EXISTS idx_series_sonarr    ON series (sonarr_id);
