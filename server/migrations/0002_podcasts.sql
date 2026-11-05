-- NULL in the optional columns means "use the default from settings".
CREATE TABLE podcasts (
    id                 TEXT PRIMARY KEY
                       CHECK (length(id) BETWEEN 1 AND 64 AND id NOT GLOB '*[^a-z0-9-]*'),
    name               TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    playlist_url       TEXT NOT NULL,
    image_url          TEXT NOT NULL DEFAULT '',
    scan_interval_min  INTEGER CHECK (scan_interval_min > 0),
    scan_depth         INTEGER NOT NULL DEFAULT 20 CHECK (scan_depth > 0),
    pause_min_s        INTEGER CHECK (pause_min_s >= 0),
    pause_max_s        INTEGER CHECK (pause_max_s >= 0),
    downloads_per_hour INTEGER CHECK (downloads_per_hour > 0),
    keep_last          INTEGER CHECK (keep_last > 0),
    max_age_days       INTEGER CHECK (max_age_days > 0),
    active             INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
