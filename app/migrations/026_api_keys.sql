-- Keys for things that are not a browser.
--
-- Every route is behind a session cookie, which a home-automation module or
-- a Stream Deck plugin cannot obtain: there is no browser to hold it and no
-- form to post. A key is per user, because a pin list is per user and an
-- integration asking "what is on tonight" has to be asking on someone's
-- behalf.
--
-- Stored hashed, like a password. A key that can be read back out of the
-- database is a password written on the wall.
CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    -- A short, non-secret prefix so a key can be told apart in a list
    -- without ever showing the key itself again.
    prefix      TEXT NOT NULL,
    key_hash    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    last_used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys (user_id);
