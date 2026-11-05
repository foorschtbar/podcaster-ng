-- One row per episode whose files were removed (retention rule, manual delete, re-download).
-- The episode row itself stays with state 'deleted' so a scan does not fetch it again.
CREATE TABLE retention_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    podcast_id TEXT NOT NULL,
    video_id   TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    reason     TEXT NOT NULL,          -- keep_last, max_age, manual, redownload
    files      INTEGER NOT NULL DEFAULT 0,
    bytes      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX retention_log_at ON retention_log (id DESC);
