-- Anonymous daily counters. No IP addresses, no raw user agents.
-- episode_id is the episode's video_id (NULL for feed requests).
CREATE TABLE hits_daily (
    day        TEXT NOT NULL,   -- UTC date, YYYY-MM-DD
    podcast_id TEXT NOT NULL,
    episode_id TEXT,
    kind       TEXT NOT NULL CHECK (kind IN ('feed', 'file')),
    app_group  TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    bytes      INTEGER NOT NULL DEFAULT 0
);
-- NULL episode_id must collide with itself, hence the expression.
CREATE UNIQUE INDEX hits_daily_key
    ON hits_daily (day, podcast_id, COALESCE(episode_id, ''), kind, app_group);
