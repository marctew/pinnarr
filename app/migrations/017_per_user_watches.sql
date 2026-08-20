-- Watch state per viewer, not per household.
--
-- episodes.watched_at recorded that *somebody* had seen an episode, which is
-- the wrong claim on a page that is otherwise entirely per user: showing
-- another person's viewing as yours is worse than showing nothing.
CREATE TABLE IF NOT EXISTS episode_watches (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    episode_id  INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    watched_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, episode_id)
);
CREATE INDEX IF NOT EXISTS idx_watches_episode ON episode_watches (episode_id);

-- Which Plex account a Pinnarr account is, so Tautulli history can be
-- attributed. Derived from the token each user already supplies.
ALTER TABLE users ADD COLUMN plex_username TEXT;

-- The household-wide column goes rather than lingering as a second, subtly
-- different answer to the same question. Its index has to go first: SQLite
-- refuses to drop a column something still indexes.
DROP INDEX IF EXISTS idx_episodes_watched;
ALTER TABLE episodes DROP COLUMN watched_at;
