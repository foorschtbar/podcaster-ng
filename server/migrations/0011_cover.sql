-- The podcast's cover image, stored in data/media/<podcast_id>/ (a bare file name). NULL: the feed falls back to image_url.
ALTER TABLE podcasts ADD COLUMN cover_file TEXT;
