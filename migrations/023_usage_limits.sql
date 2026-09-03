-- Additive migration for the usage-limits/subscription-plans system (see
-- usage_limits_proposal.md for the full design). Adds:
--
--   * users.plan_id -- a free-text key into messa/plans.py's PLANS dict,
--     not a Postgres ENUM. Deliberately NOT extending the existing
--     `user_tier` enum (users.tier, 'free'/'pro') -- that column predates
--     this feature, was never wired into any code, and is left alone
--     rather than repurposed, since growing an ENUM's value set requires
--     a migration every time, which fights the whole point of "plans are
--     data, defined in one Python file." plan_id is validated against
--     plans.ACTIVE_PLANS at the application layer instead, the same way
--     other free-text "pick one of these known values" columns already
--     work in this schema (e.g. default_email_provider's provider check).
--
--   * users.is_admin -- a quiet, unadvertised bypass for the dev team's
--     own accounts. Deliberately a plain boolean, not a plan_id value or
--     anything that could ever surface on the pricing page: flip it with
--     a manual `UPDATE users SET is_admin = true WHERE phone_number = ...`,
--     nothing discoverable, nothing else in the schema references it.
--
--   * usage_daily_counts -- one row per user per feature per day. `day` is
--     computed by the application in the user's own confirmed timezone
--     (not server UTC), so a "N/day" limit actually resets at that user's
--     own midnight. Every check-and-consume is a single atomic
--     INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING count (see
--     messa/db.py's check_and_increment_usage), so concurrent tool calls
--     in the same turn can't both slip past a limit via a read-then-write
--     race. Rows are written for every user, admins included -- the count
--     is kept for cost visibility even on an account where nothing is
--     ever actually enforced.

ALTER TABLE users ADD COLUMN IF NOT EXISTS plan_id VARCHAR(50) NOT NULL DEFAULT 'basic';
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS usage_daily_counts (
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    feature VARCHAR(50) NOT NULL,
    day DATE NOT NULL,
    count INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, feature, day)
);
