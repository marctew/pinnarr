-- Last observed state of each pin/tag pair, so the two-way sync can tell
-- which side changed. Without it, "pinned here but not tagged there" is
-- ambiguous: it means either "pin it in Sonarr" or "unpin it here", and
-- guessing wrong silently undoes whatever someone just did.
CREATE TABLE IF NOT EXISTS tag_sync_state (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    series_id  INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    pinned     INTEGER NOT NULL,
    tagged     INTEGER NOT NULL,
    synced_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, series_id)
);
