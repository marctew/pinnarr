-- When a notification does not arrive there is currently no way to tell
-- whether the job never fired, ntfy refused it, or the phone ate it. The
-- first two are knowable and were simply not being written down.
--
-- The body is stored as sent. A log that records "an arrival notification"
-- without its text cannot answer "did it tell me about the right episode".
CREATE TABLE IF NOT EXISTS notification_log (
    id       INTEGER PRIMARY KEY,
    user_id  INTEGER REFERENCES users(id) ON DELETE CASCADE,
    topic    TEXT,
    kind     TEXT NOT NULL,
    title    TEXT NOT NULL,
    body     TEXT NOT NULL,
    ok       INTEGER NOT NULL,
    detail   TEXT,
    sent_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notification_log_user ON notification_log (user_id, id DESC);
