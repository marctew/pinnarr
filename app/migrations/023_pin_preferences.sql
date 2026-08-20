-- Two per-pin decisions that only make sense per person, so they live on the
-- pin rather than the series.
--
-- Snooze: a show on hiatus produces no episodes but stays in every list and
-- every alert. Unpinning loses the pin; muting keeps it.
--
-- Season packs: notify_batch_minutes already groups an import that takes ten
-- minutes into one push. This is the same idea at season scale, for shows
-- you would rather binge than follow weekly.
ALTER TABLE pins ADD COLUMN snoozed_until      TEXT;
ALTER TABLE pins ADD COLUMN snooze_until_dated INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pins ADD COLUMN season_pack        INTEGER NOT NULL DEFAULT 0;
