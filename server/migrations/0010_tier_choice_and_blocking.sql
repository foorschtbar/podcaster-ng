-- A manual retry can start a task at a chosen tier: only workers of that tier (and, once it escalates, above) see it.
ALTER TABLE tasks ADD COLUMN min_tier INTEGER NOT NULL DEFAULT 1;
-- A worker that keeps getting bot checks stops taking downloads (scans go on) until this time.
ALTER TABLE workers ADD COLUMN download_blocked_until TEXT;
