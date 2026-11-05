-- Escalation across worker tiers, pause/reopen of stuck tasks, and the Telegram outbox.
ALTER TABLE tasks ADD COLUMN tier_since TEXT;                                  -- when the task last became available to its current tier (NULL: created_at)
ALTER TABLE tasks ADD COLUMN reset_attempt_id INTEGER NOT NULL DEFAULT 0;      -- failure accounting counts attempts after this one
ALTER TABLE tasks ADD COLUMN reopen_at TEXT;                                   -- a paused task reopens by itself at this time
ALTER TABLE tasks ADD COLUMN pause_count INTEGER NOT NULL DEFAULT 0;

CREATE TABLE task_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,        -- escalated, paused, reopened
    detail  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX task_events_task ON task_events (task_id);

-- Messages to send. The unique key makes every condition produce at most one message.
CREATE TABLE notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'skipped', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_try_at TEXT,
    last_error  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    sent_at     TEXT
);
CREATE INDEX notifications_status ON notifications (status, id);

ALTER TABLE workers ADD COLUMN offline_alerted_at TEXT;                        -- set while an "offline" message has been sent
