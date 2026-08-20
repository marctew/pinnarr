-- Who is in what, so a face can be traced across your own shelf.
--
-- Every other tool answers "where do I know them from" with an IMDb page:
-- eighty credits, three of which you have ever seen. The useful answer is
-- the intersection with what you actually own, and Pinnarr is the only thing
-- here that knows both halves.
CREATE TABLE IF NOT EXISTS people (
    tmdb_person_id INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    profile_path   TEXT,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series_cast (
    series_id      INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    tmdb_person_id INTEGER NOT NULL REFERENCES people(tmdb_person_id) ON DELETE CASCADE,
    character      TEXT,
    -- How much of the show they are actually in. TMDB lists a one-scene
    -- guest alongside the lead, and "you have seen them before" is only
    -- interesting for someone you would recognise.
    episode_count  INTEGER NOT NULL DEFAULT 0,
    billing        INTEGER NOT NULL DEFAULT 999,
    PRIMARY KEY (series_id, tmdb_person_id)
);
CREATE INDEX IF NOT EXISTS idx_series_cast_person ON series_cast (tmdb_person_id);
