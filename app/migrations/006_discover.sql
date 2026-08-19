-- When we last pulled the full episode list for a series from Sonarr.
-- The nightly calendar sync only covers -7 to +60 days, so without this
-- there is no way to tell "no episodes exist" from "none are near".
ALTER TABLE series ADD COLUMN episodes_synced_at TEXT;
