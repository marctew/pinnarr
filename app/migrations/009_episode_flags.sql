-- Sonarr labels finales itself (finaleType: "season" | "series"), which beats
-- guessing from the highest episode number we happen to hold — the calendar
-- window is only -7 to +60 days, so that guess would be wrong most of the time.
ALTER TABLE episodes ADD COLUMN finale_type TEXT;

-- Air dates move. Sonarr updates them quietly and nothing tells you, so a
-- finale slipping a week is invisible until it fails to turn up.
CREATE TABLE IF NOT EXISTS schedule_changes (
    id          INTEGER PRIMARY KEY,
    episode_id  INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    old_date    TEXT,
    new_date    TEXT,
    detected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_changes_episode ON schedule_changes (episode_id);

-- Per user, because pins are.
CREATE TABLE IF NOT EXISTS change_notifications (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    change_id   INTEGER NOT NULL REFERENCES schedule_changes(id) ON DELETE CASCADE,
    notified_at TEXT NOT NULL,
    PRIMARY KEY (user_id, change_id)
);
