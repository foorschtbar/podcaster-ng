-- What the worker is doing right now ("downloading", "uploading audio 40%"); sent with the heartbeat.
ALTER TABLE tasks ADD COLUMN progress TEXT NOT NULL DEFAULT '';
