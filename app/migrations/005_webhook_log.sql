-- The Sonarr webhook payload is undocumented (SPEC §17, open question 1), so
-- every delivery is kept with its raw body. The parser can then be written
-- against what actually arrives rather than against a guess.
CREATE TABLE IF NOT EXISTS webhook_log (
    id           INTEGER PRIMARY KEY,
    received_at  TEXT NOT NULL,
    event_type   TEXT,
    handled      INTEGER NOT NULL DEFAULT 0,
    detail       TEXT,
    body         TEXT
);
