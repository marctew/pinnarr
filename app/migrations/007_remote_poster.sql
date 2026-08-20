-- Poster art for series Plex does not hold. poster_url is a Plex thumb path
-- and only exists for shows already in the library, which left everything
-- Sonarr is merely tracking — and most of Discover — with a blank card.
ALTER TABLE series ADD COLUMN remote_poster TEXT;
