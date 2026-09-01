-- Additive migration: lets a user's resolved geographic coordinates be
-- stored alongside their timezone, so messa/weather.py can call Open-
-- Meteo's forecast API directly (lat/lon in, no separate geocoding round
-- trip at briefing-render time) rather than re-resolving location on every
-- morning/evening briefing.
--
-- Populated by the exact same place timezone already gets resolved --
-- timeutil.resolve_timezone (Open-Meteo's free, keyless geocoding API,
-- which already returns latitude/longitude on every result, previously
-- just discarded after picking a timezone) -- via db.update_user_timezone,
-- called from db.ensure_timezone_resolved and db.save_profile_field's
-- city-save branch. NULL for any user who hasn't given a resolvable city
-- yet (never onboarded past that step, said "skip", or gave something
-- Open-Meteo's geocoder couldn't match) -- messa/briefings.py drops the
-- weather line entirely for those users rather than guessing a location.

ALTER TABLE users ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION;
ALTER TABLE users ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION;
