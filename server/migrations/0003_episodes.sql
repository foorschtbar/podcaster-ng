-- Channel author (the old Podcaster took it from the playlist's meta.info.json).
ALTER TABLE podcasts ADD COLUMN author TEXT NOT NULL DEFAULT '';

-- One row per known video. State 'deleted' without files means "known, do not download again".
-- File columns hold bare file names inside data/media/<podcast_id>/.
CREATE TABLE episodes (
    podcast_id    TEXT NOT NULL REFERENCES podcasts(id) ON DELETE RESTRICT,
    video_id      TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    author        TEXT NOT NULL DEFAULT '',
    audio_file    TEXT,
    thumb_file    TEXT,
    square_file   TEXT,
    chapters_file TEXT,
    info_file     TEXT,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    duration_s    INTEGER NOT NULL DEFAULT 0,
    published_at  TEXT,
    state         TEXT NOT NULL DEFAULT 'available' CHECK (state IN ('available', 'hidden', 'deleted')),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (podcast_id, video_id)
);
CREATE INDEX episodes_podcast_state ON episodes (podcast_id, state);
