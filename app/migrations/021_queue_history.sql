-- The queue was replaced wholesale every minute, so no row was ever older
-- than one tick and "has this moved?" was unanswerable. A download stuck at
-- 3% looked exactly like one that had only just started.
--
-- Keeping rows across ticks costs nothing and makes stalling visible: the
-- percentage is only stamped when it actually changes.
ALTER TABLE download_queue ADD COLUMN first_seen_at TEXT;
ALTER TABLE download_queue ADD COLUMN progress_at TEXT;
