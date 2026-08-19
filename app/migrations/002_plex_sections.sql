-- Section names, so the library facet can say "Anime" rather than "5".
-- Populated by the plex_library job, which already walks the section list.
CREATE TABLE IF NOT EXISTS plex_sections (
    id     INTEGER PRIMARY KEY,
    title  TEXT NOT NULL,
    type   TEXT,
    agent  TEXT,
    seen_at TEXT NOT NULL
);
