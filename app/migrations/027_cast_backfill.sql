-- Cast was pulled for pinned shows only, which quietly gutted the feature it
-- was built for. "Where do I know them from" needs credits on *both* shows,
-- and with a dozen pins that only ever finds actors appearing in two of the
-- twelve. The shows most likely to answer the question are the ones watched
-- years ago and never pinned — which had no credits at all, so they could
-- never surface.
--
-- Stamping each series lets the job cover the whole library a batch at a
-- time instead of all at once, and top up only what is missing afterwards.
ALTER TABLE series ADD COLUMN cast_synced_at TEXT;
CREATE INDEX IF NOT EXISTS idx_series_cast_synced ON series (cast_synced_at);
