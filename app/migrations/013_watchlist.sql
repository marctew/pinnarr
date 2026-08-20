-- The Plex Watchlist belongs to a Plex account, not to a server, so each
-- Pinnarr account carries its own token and syncs against its own list.
ALTER TABLE users ADD COLUMN plex_token TEXT;

-- Watchlist entries are Plex Discover objects keyed on plex:// GUIDs. We
-- were parsing those out for their tvdb/tmdb ids and throwing the GUID away;
-- keeping it means matching needs no extra lookup per item.
ALTER TABLE series ADD COLUMN plex_guid TEXT;
CREATE INDEX IF NOT EXISTS idx_series_plex_guid ON series (plex_guid);

-- Same shape as tag_sync_state, and for the same reason: without the last
-- observed state, "pinned here but not listed there" cannot be told from
-- "listed there but not pinned here".
CREATE TABLE IF NOT EXISTS watchlist_sync_state (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    series_id  INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    pinned     INTEGER NOT NULL,
    listed     INTEGER NOT NULL,
    synced_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, series_id)
);
