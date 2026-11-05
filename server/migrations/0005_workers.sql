-- Only the SHA-256 hash of the token is stored. NULL pacing values mean "use the default from settings".
CREATE TABLE workers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL COLLATE NOCASE UNIQUE,
    token_hash         TEXT NOT NULL UNIQUE,
    tier               INTEGER NOT NULL DEFAULT 1 CHECK (tier BETWEEN 1 AND 99),
    active             INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    last_seen          TEXT,          -- UTC, YYYY-MM-DDTHH:MM:SSZ
    version            TEXT NOT NULL DEFAULT '',
    capabilities       TEXT NOT NULL DEFAULT '[]',   -- JSON list
    tools              TEXT NOT NULL DEFAULT '{}',   -- JSON object: tool name -> version
    pause_min_s        INTEGER CHECK (pause_min_s >= 0),
    pause_max_s        INTEGER CHECK (pause_max_s >= 0),
    downloads_per_hour INTEGER CHECK (downloads_per_hour > 0),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
