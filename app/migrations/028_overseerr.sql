-- Requesting things you do not own.
--
-- The recommendations table has always held every show TMDB suggested,
-- owned or not; `suggested()` threw the unowned ones away at query time on
-- the grounds that "a recommendation you cannot watch is an advert". With
-- somewhere to send a request that stops being true, so the rest of the
-- row is worth keeping: without a poster and a date an unowned suggestion
-- is a bare string.
ALTER TABLE recommendations ADD COLUMN poster_path TEXT;
ALTER TABLE recommendations ADD COLUMN first_air_date TEXT;
ALTER TABLE recommendations ADD COLUMN overview TEXT;

-- What Overseerr says about a TMDB id. Cached rather than asked per card:
-- a Discover page of thirty suggestions would otherwise be thirty calls to
-- draw one screen.
CREATE TABLE IF NOT EXISTS overseerr_media (
    tmdb_id      INTEGER PRIMARY KEY,
    status       TEXT NOT NULL,
    requested_by TEXT,
    updated_at   TEXT NOT NULL
);

-- Overseerr's key is a single admin credential, so a request made with it is
-- attributed to whoever owns it unless a userId is supplied. Pinnarr has
-- real accounts; Kate asking for something should not appear as you.
ALTER TABLE users ADD COLUMN overseerr_user_id INTEGER;
