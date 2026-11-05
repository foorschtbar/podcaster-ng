-- What a worker reports about its machine (JSON of the worker API's Device), when, and the address its calls came from.
ALTER TABLE workers ADD COLUMN device TEXT NOT NULL DEFAULT '{}';
ALTER TABLE workers ADD COLUMN device_at TEXT;
ALTER TABLE workers ADD COLUMN last_ip TEXT NOT NULL DEFAULT '';
