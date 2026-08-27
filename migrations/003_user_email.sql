-- Additive migration: adds an email column so Messa's onboarding flow
-- (name -> email [skippable] -> location, driven by users.onboarding_step,
-- which was already in your schema) has somewhere to put it.
-- Safe to re-run; the app also works without this (db.py checks for the
-- column first and just skips storing email until you run this).

ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255);
