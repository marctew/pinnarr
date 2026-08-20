-- When the availability job last confirmed a series against Plex.
--
-- Without this, "Plex has none of these" and "we have never asked Plex about
-- this" are the same value — zero — and the cross-check reports a series as
-- missing from Plex purely because it was pinned an hour ago.
ALTER TABLE series ADD COLUMN plex_checked_at TEXT;
