-- Sonarr's web UI routes by slug, not by id, so a link to a series needs it.
ALTER TABLE series ADD COLUMN title_slug TEXT;
