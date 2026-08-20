-- Per-episode watch state. Tautulli only ever gave us one timestamp per
-- series, for library sorting, so Ready to Watch could never strike anything
-- off — marking something watched in Plex changed nothing here.
ALTER TABLE episodes ADD COLUMN watched_at TEXT;
CREATE INDEX IF NOT EXISTS idx_episodes_watched ON episodes (watched_at);

-- The availability job stamped arrived_at the first time it saw an episode
-- in Plex, which is "when Pinnarr looked", not "when it landed" — the same
-- fault fixed in upsert_episode and missed here. Every existing value is
-- suspect and none can be told from a real one, so they go; Ready falls back
-- to the air date, which is right for a back catalogue.
UPDATE episodes SET arrived_at = NULL
WHERE arrived_at IS NOT NULL
  AND air_date_utc IS NOT NULL
  AND julianday(arrived_at) - julianday(air_date_utc) > 2;
