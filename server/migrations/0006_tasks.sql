-- Work queue. A 'scan' task reads one podcast's playlist; a 'download' task fetches one video.
-- State 'closed' = finished without result (private/removed video, or closed by hand).
CREATE TABLE tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT NOT NULL CHECK (type IN ('scan', 'download')),
    podcast_id  TEXT NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
    video_id    TEXT,
    title       TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'claimed', 'done', 'paused', 'closed')),
    tier        INTEGER NOT NULL DEFAULT 1,      -- current tier; escalation arrives with the alerting step
    not_before  TEXT,                            -- UTC; premieres, retry backoff
    claimed_by  INTEGER REFERENCES workers(id) ON DELETE SET NULL,
    lease_until TEXT,
    pause_reason TEXT,
    failures    INTEGER NOT NULL DEFAULT 0,      -- failed attempts incl. expired leases
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    finished_at TEXT,
    CHECK ((type = 'scan' AND video_id IS NULL) OR (type = 'download' AND video_id IS NOT NULL))
);
-- At most one open scan per podcast, and one download task per video (any state, so closed or
-- removed videos are not queued again by every scan; they are reopened with "retry").
CREATE UNIQUE INDEX tasks_open_scan ON tasks (podcast_id)
    WHERE type = 'scan' AND state IN ('pending', 'claimed', 'paused');
CREATE UNIQUE INDEX tasks_download_video ON tasks (podcast_id, video_id) WHERE type = 'download';
CREATE INDEX tasks_state ON tasks (state, type, id);

CREATE TABLE attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    worker_id   INTEGER REFERENCES workers(id) ON DELETE SET NULL,
    worker_name TEXT NOT NULL DEFAULT '',        -- kept when the worker is deleted
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    result      TEXT CHECK (result IN ('ok', 'failed', 'lease_expired', 'deferred', 'released')),  -- NULL while running
    error_class TEXT,
    error_text  TEXT NOT NULL DEFAULT '',
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX attempts_task ON attempts (task_id);
CREATE INDEX attempts_worker_start ON attempts (worker_id, started_at);
