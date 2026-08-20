-- arrived_at used to be stamped the first time an episode was seen with a
-- file, which is "when Pinnarr noticed" rather than "when it landed". Loading
-- a six-season back catalogue therefore marked every episode as arriving
-- today, and Ready to Watch believed it.
--
-- Every existing value was produced that way, and none can be told apart from
-- a real one after the fact, so they all go. NULL is excluded from the
-- notification queries, so this cannot cause a storm — Ready to Watch is
-- simply empty until something genuinely arrives, which is the honest answer.
UPDATE episodes SET arrived_at = NULL;
