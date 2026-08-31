-- Additive migration: lets a saved contact (the `people` table, from the
-- original base schema -- see neon-schema.sql, never touched again after
-- initial creation per this project's additive-only migration philosophy)
-- carry a phone number and email address, not just a name/relationship/
-- notes. This is what lets Messa actually resolve "email Sam about the
-- invoice" or "text Jane I'm running late" to a real address/number
-- without asking the user to repeat it every time -- see
-- db.find_person_by_name and the orchestrator's new find_contact tool
-- (agents/registry.py).
--
-- Both nullable: an existing contact saved before this migration has
-- neither on file yet (nothing to backfill from), and a new contact can
-- still be saved with just a name, same as always -- phone_number/email
-- are filled in whenever the user happens to mention them, not required
-- up front.

ALTER TABLE people ADD COLUMN IF NOT EXISTS phone_number VARCHAR(32);
ALTER TABLE people ADD COLUMN IF NOT EXISTS email VARCHAR(320);
