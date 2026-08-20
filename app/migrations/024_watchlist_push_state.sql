-- "Pinned here, not listed there, and neither side changed since we last
-- looked" has two causes that look identical: we pushed and Plex ignored it,
-- or we never managed to push at all. Both end with us pushing again, every
-- ten minutes, forever — which is what "I remove it from my watchlist and it
-- comes straight back" actually is.
--
-- Recording that we already tried breaks the tie. One attempt, then stop and
-- say so, rather than argue with Plex on a timer.
ALTER TABLE watchlist_sync_state ADD COLUMN pushed_at TEXT;
