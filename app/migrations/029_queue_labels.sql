-- The queue page listed only pinned shows, and only episodes Pinnarr had a
-- row for. Both were invisible filters on a page whose job is "what is
-- Sonarr doing" — and the second one cannot be fixed by relaxing a join,
-- because a download for an episode outside the synced calendar window has
-- nothing local to name it.
--
-- So the queue row carries its own labels, straight from Sonarr.
ALTER TABLE download_queue ADD COLUMN series_title  TEXT;
ALTER TABLE download_queue ADD COLUMN episode_title TEXT;
ALTER TABLE download_queue ADD COLUMN season        INTEGER;
ALTER TABLE download_queue ADD COLUMN episode       INTEGER;
