-- Plex's own id for an episode, so "in Plex" can open the episode rather
-- than the series. Captured by the watch-state sweeps, which already read
-- exactly this data.
ALTER TABLE episodes ADD COLUMN plex_rating_key TEXT;
