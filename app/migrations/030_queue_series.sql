-- The queue could only reach a series through its episode, and Pinnarr only
-- holds episodes inside the synced calendar window — so a download for
-- anything older had no link to the show page, however well Pinnarr knew it.
--
-- Sonarr has been sending the series id all along; QueueItem has carried it
-- since the sync was written and nothing ever wrote it down.
ALTER TABLE download_queue ADD COLUMN sonarr_series_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_queue_series ON download_queue (sonarr_series_id);
