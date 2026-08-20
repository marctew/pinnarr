-- Retire deletes pins, so there was nothing left to undo from. The page
-- offered a one-click bulk unpin of everything it listed and returned a
-- batch id the browser could not redeem — bulk pin had a working undo, and
-- the far more destructive action did not.
--
-- Keeping the removed rows here rather than soft-deleting in `pins` means
-- every existing query is untouched: `pins` still means "pinned right now".
CREATE TABLE IF NOT EXISTS retired_pins (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    series_id  INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    batch      TEXT NOT NULL,
    pinned_at  TEXT NOT NULL,
    notify     INTEGER NOT NULL DEFAULT 1,
    retired_at TEXT NOT NULL,
    PRIMARY KEY (user_id, series_id, batch)
);
CREATE INDEX IF NOT EXISTS idx_retired_batch ON retired_pins (user_id, retired_at DESC);
