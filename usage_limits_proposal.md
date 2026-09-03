# Usage limits & subscription plans -- implementation plan

## What this covers

A generalized, config-driven system for metering a handful of costed
actions (outbound emails, browser actions, outbound texts, how many
apps are connected, and anything else added later), enforcing a daily cap
or a standing ceiling per user based on which plan they're on, and telling
them to upgrade -- conversationally, through Messa herself -- when they
hit it. It also covers a quiet, unadvertised admin bypass for your own
team, and a fast-iteration path for editing plans and prices from a
Hugging Face Spaces variable without a code push. Stripe isn't wired in
this round (you said "soon"), but the design leaves an explicit, empty
slot for it so turning it on later is a config edit, not a
rearchitecture. This is a proposal to review, updated with your latest
feedback -- not code that's been written yet.

## The core idea: plans are data, not code

Everything about "what plans exist and what each one allows" lives in one
new file, `messa/plans.py`, following the exact pattern `config.py` already
uses everywhere else in this codebase (plain Python constants, heavily
commented, env-override-able where that makes sense). Something like:

```python
# messa/plans.py
from dataclasses import dataclass, field

@dataclass(frozen=True)
class PlanLimits:
    # None means unlimited for that feature on this plan.
    outbound_emails: int | None
    browse_actions: int | None
    number_of_texts: int | None
    max_connected_apps: int | None

@dataclass(frozen=True)
class Plan:
    id: str                 # stored in users.plan_id, never renamed once live
    name: str                # what Messa/the pricing page shows
    price_cents: int          # 0 for free
    limits: PlanLimits
    stripe_payment_link: str | None = None   # filled in once Stripe exists

PLANS: dict[str, Plan] = {
    "basic": Plan(
        id="basic", name="Basic", price_cents=0,
        limits=PlanLimits(outbound_emails=10, browse_actions=5, number_of_texts=25),
    ),
    "pro": Plan(
        id="pro", name="Pro", price_cents=1200,
        limits=PlanLimits(outbound_emails=50, browse_actions=25, number_of_texts=150),
    ),
    # "plus" / "business" tiers slot in here once you've decided on them --
    # see the pricing section below for a first-pass recommendation.
}
DEFAULT_PLAN_ID = "basic"
```

Adding a plan, changing a limit, or making a feature unlimited on some
tier is a one-line edit here -- no migration, no redeploy of anything but
this file. This directly matches what you described: "a file where I can
type what plans I offer."

## Making it editable from Hugging Face Spaces, without a wall of variables

You're right that one env var per number (four plans times four limits
times a price -- twenty-plus variables, growing every time a feature or
plan is added) would be unpleasant to maintain and easy to get out of
sync with itself. The fix is to keep the *shape* in code (the dataclasses
above, checked into git, so there's always a real history of pricing
changes) but let a single optional environment variable, say
`MESSA_PLANS_OVERRIDE_JSON`, replace the whole `PLANS` dict at once with a
JSON blob shaped the same way. That's one field to paste into on Hugging
Face Spaces -- edit it, save, the Space restarts, new plans and prices are
live in seconds with no code push and no migration. This is the same
`os.environ.get(...)` idiom `config.py` already uses everywhere, just
applied to one structured value instead of many scalar ones. If the
variable is unset, the code-defined `PLANS` above is what runs -- so
Hugging Face becomes a fast override switch, not the only copy of your
pricing, and you're never stuck if you clear the variable by mistake.

The one thing this needs to be reliable rather than a foot-gun: the JSON
is parsed and validated exactly once, at process startup, not per
request. If it's malformed or missing a required field, the app fails
loudly at boot with a specific error naming the bad field, rather than
silently falling back mid-flight to defaults that don't match what the
pricing page shows, or crashing on some unlucky request three hours
later. This also settles how "unlimited" gets spelled in the override:
JSON's `null` deserializes straight to Python's `None`, so the override
format matches the in-code convention exactly -- `"number_of_texts": null`
means unlimited, no separate sentinel value to remember or get wrong.

## Where a user's plan lives

The schema already has a `user_tier` ENUM (`'free'`, `'pro'`) on `users.tier`
-- created in `neon-schema.sql` but never referenced anywhere in the code
(confirmed: zero hits for `.tier`/`user_tier` outside the schema file). It's
tempting to reuse it, but I'd recommend against extending it: Postgres
ENUMs are awkward to grow at the pace you're iterating on plan names/count
right now (adding a value is a schema migration every time, and dropping
one is worse), and you explicitly haven't settled on plan 3 and 4 yet.

Proposed instead: a new migration adds `users.plan_id VARCHAR(50) NOT NULL
DEFAULT 'basic'`, a free-text foreign key into the `PLANS` dict above
rather than the database. `plan_id` values are validated at the
application layer against `plans.PLANS` (same instinct as everywhere else
in this codebase that already validates against Python constants instead
of a DB enum, e.g. `default_email_provider`'s allowed-provider list). The
old `tier` column is left alone -- harmless, undocumented as deprecated in
the README the way every other deviation here is recorded.

## Counting usage: one new table

A new `usage_daily_counts` table, day-bucketed per user per feature:

```sql
CREATE TABLE usage_daily_counts (
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    feature VARCHAR(50) NOT NULL,   -- 'outbound_emails' | 'browse_actions' | 'number_of_texts' | ...
    day DATE NOT NULL,
    count INT NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, feature, day)
);
```

`day` is computed in the user's own confirmed timezone (falling back to
UTC if `timezone_confirmed` is false, same fallback `UserContext` already
uses elsewhere) rather than server UTC, so "10/day" actually resets at the
user's own midnight, not an arbitrary server cutoff. A single new
`db.check_and_increment_usage(user_id, feature, day, limit) -> (allowed:
bool, count_after: int)` does the read-check-write as one atomic
`INSERT ... ON CONFLICT (user_id, feature, day) DO UPDATE SET count =
usage_daily_counts.count + 1 WHERE ... RETURNING count` statement, so two
concurrent tool calls in the same turn can't both slip through past the
cap.

## Admin mode: a quiet bypass, not a plan

This should not be a `Plan` in `plans.py` at all -- you're right that it
shouldn't appear anywhere a user or the pricing page could ever see it.
Instead: a new `users.is_admin BOOLEAN NOT NULL DEFAULT false` column,
completely separate from `plan_id`. Exactly one function in the whole app
-- the same `check_and_increment_usage` gate everything else in this
proposal routes through -- checks it, first, before consulting `plans.py`
at all: if `is_admin` is true, the action is always allowed. That's the
only place this check needs to exist; every hook point described below
calls into that one function rather than re-implementing the check, so
there's exactly one bug surface for "does admin actually bypass
everything," not five. I'd still increment the usage counters for admin
accounts rather than skip them entirely -- not to enforce anything, but
because your own dev-team usage is real OpenRouter/Browserbase cost, and
this table is the cheapest way to see what your own team is spending
without digging through provider dashboards. Flipping the flag on for a
teammate is a single manual `UPDATE users SET is_admin = true WHERE
phone_number = '...'` -- no admin UI, nothing discoverable, exactly the
"secretly modify this in the backend" mechanism you described.

## Hooking it into the four places usage actually happens

This is the part I spent most of the research on, because the four
"costed" actions don't all go through the same code path, and pretending
they do would produce a design that silently misses two of them.

**Messa's own tools (calendar, tasks, personal-inbox email, Gmail via
`email_tools.py`) all funnel through one place**: `trace_tool()` in
`messa/tools/common.py`, the single wrapper every `@tool`-decorated
function in this app already passes through before it runs, right
alongside the existing destructive-action approval gate. This is the
natural, already-there hook: extend `build_*_tools(user, approval_gate=...)`
to also accept a `usage_limiter` object (same shape as the existing
`ApprovalGate` Protocol -- a `check_and_consume(user, feature) ->
LimitResult` method), and let `trace_tool` accept an optional `feature`
tag per tool it wraps. Messa's own `send_email`-equivalent tools and
Gmail's send tool both get tagged `feature="outbound_emails"`; nothing else
changes about how those functions work.

**Third-party email sent through the generic integration path is the one
tricky case.** `integration_tools.py`'s `execute_integration_tool` is a
single dispatcher that can run any of 1,400+ Composio actions by slug --
there's no separate "Outlook send email" tool to tag, just one generic
`execute_integration_tool(slug, arguments)` that happens to be called with
`slug="OUTLOOK_SEND_EMAIL"` sometimes. So the check for this path has to
live inside `execute_integration_tool` itself, matching the incoming
`slug` against a small explicit set of known send-email action slugs
(`OUTLOOK_SEND_EMAIL`, etc. -- an explicit list, not substring-matching
"SEND", since Composio's naming isn't perfectly consistent and a fuzzy
match risks either false-counting an unrelated action or missing a real
one). When it matches, it consumes from the same `outbound_emails` counter
as Messa's own and Gmail's paths -- one logical limit, three physical code
paths feeding it, exactly as you described it in your example.

**Browser actions**: the natural unit here is a browsing session, not
individual clicks. `BrowserToolProvider` (`deepsearch_tools.py`) owns one
Browserbase session per top-level task -- delegated sub-workers
(`delegate_website_task`) share that same session rather than opening
their own. Counting at `BrowserToolProvider.__aenter__`, once per
top-level session (never for a sub-worker connecting to an
already-running session), both matches the plain-language meaning of "5
browse actions a day" as "5 times you can send me off to go browse
something" and matches what Browserbase actually bills you for -- session
time, not click count.

**Outbound texts are the odd one out**: they're not a LangChain tool at
all. Every SMS/iMessage Messa sends -- including a multi-message reply --
goes through one callback, `_sms_send_factory`'s inner `_send(text)` in
`server.py`. That's the hook: a manual `check_and_increment_usage` call
right before the actual Sendblue API call, not through `trace_tool`.

## A fifth limit: how many apps a user can connect at once

Good instinct, and worth calling out because it's a genuinely different
*kind* of limit from the three above -- it's not a daily rate that resets
at midnight, it's a ceiling on how many things can be true *right now*
(how many Composio connections plus Gmail are currently active). That's
also what makes it easy to enforce reliably: no new counting table is
needed at all. `db.get_active_connected_toolkits(user_id)` already exists
and already reads Composio's live connected-accounts list on demand, so
there's nothing that can go stale the way a cached counter could --
checking "is this user at their cap" is just "is `len(connected) >=
plan.limits.max_connected_apps`" (Gmail counted the same way as any
Composio toolkit, since it's also "an app" from the user's point of view),
evaluated fresh every time. This slots into `connect_integration_app` and
Gmail's own connection-request tool in `email_tools.py`, both already
gated the same way destructive actions are -- one more condition
alongside the existing checks, not a new mechanism. It also gives you
exactly the lever you're describing: a real ceiling that still means
something even in a future where messaging itself is unlimited. A
first-pass split across the tiers, open to your numbers: Basic 2, Pro 6,
Plus 15, Business unlimited (`None`, same convention as every other
limit).

## The chicken-and-egg problem, resolved

If literally every outbound text counts, a user who's hit their daily cap
can't even be told they've hit it -- telling them costs one more text.
Proposed resolution: the "you're at your limit, here's how to upgrade"
notice is sent through a separate path that does *not* increment
`number_of_texts` (it's a system notice, not a Messa-authored reply), and
it's sent at most once per user per day -- tracked with a small
`last_limit_notice_sent_at` timestamp column (or reuse the same
`usage_daily_counts` row with a dedicated `feature='limit_notice'` entry,
which avoids a schema change). After that one notice, further inbound
texts that same day are still logged (never dropped -- see below) but
Messa goes quiet on them rather than repeating "you're over your limit"
on every message, which would read as spammy and add no new information.

Importantly, this check happens *before* the agent is invoked at all --
at the very top of `cli.run_message`, right where the inbound message
first arrives -- not after an LLM run that then gets its reply discarded.
A blocked message costs zero model calls and zero tool calls, which
matters for both of your stated goals: it's the cheapest possible way to
enforce the cap (no wasted OpenRouter spend on a reply nobody will see),
and it's the fastest, since a blocked message returns instantly instead of
waiting on a full agent loop that was never going to produce a delivered
reply anyway.

For the other two features (email, browsing), the failure mode is gentler
since it's request-triggered rather than reply-triggered: `trace_tool`'s
existing pattern of turning an exception into a string the model reacts to
extends naturally -- a blocked action returns something like "Daily limit
for outbound_emails reached (10/10) on the Basic plan. Let the user know
they can upgrade at the link below for a higher limit," and the LLM relays
that conversationally, the same way it already relays any other tool
error today. No new hardcoded Messa sentence needed.

## Blocked messages aren't lost -- but they shouldn't ambush the model either

Every inbound text is still written to `message_history` unconditionally,
whether or not Messa was able to reply to it -- this table is your record
of what actually happened, and silently dropping rows out of it would
make debugging and support conversations much harder later ("did you even
get my text?" should always have a real answer). So yes, exactly as you
guessed: once the limit resets, `get_recent_messages` will pull those old,
never-answered messages back into context on the user's next allowed
turn, right alongside their new one.

Whether that's good or bad depends on whether the model knows what it's
looking at. Left alone, a run of consecutive user messages with no
assistant reply in between reads to the model like a backlog it now has
to individually answer -- which can produce an odd wall of delayed
replies to questions that may no longer be relevant hours later. The fix
doesn't need a schema change: `message_history`'s `role` column already
supports `'system'` as a first-class value (defined in the enum,
unused by this feature today). When the limit is hit, one system-role row
gets appended -- something like "the user was over their daily message
limit starting at 11:47pm; the messages below arrived during that window
and were not responded to in real time." When context is next loaded, the
model sees that marker and can treat what follows as backlog to
acknowledge briefly rather than a stack of live questions to answer one
by one. This is a small, cheap addition (one extra row, written once per
limit window, not per blocked message) that turns "is this good or bad"
into a design choice you're making on purpose rather than an accident of
how history happens to get replayed.

## The upgrade path, before Stripe exists

Since a real payment page and Stripe links are explicitly "soon," this
round only needs a placeholder that costs almost nothing to build and
nothing to throw away later: a static pricing page rendered the same way
the marketing landing page already is (`landing_page.py`'s
token-substitution pattern), reading plan names/prices/limits straight out
of `plans.PLANS` -- so the page and what Messa tells a user in a text can
never drift out of sync, since they're reading the same file. Each plan's
call-to-action is either a `mailto:` link or, arguably more in character
for a texting product, a "text UPGRADE PRO to this number" flow you
approve manually for now -- flagging this as a real product decision worth
making rather than defaulting silently, since a web checkout button feels
like a strange detour for a product whose whole interface is SMS. Once
Stripe is ready, a plan's `stripe_payment_link` field stops being `None`
and the page/text flow both pick it up automatically; a webhook handler
that flips `users.plan_id` on a successful Stripe payment is the only new
code needed at that point, and nothing here needs to change to accommodate
it.

## Pricing: a first pass, with the honest caveat up front

Your fixed monthly costs, independent of how many users you have, add up
to roughly $9 (Huggingface) + $100 (Sendblue) + $99 (Browserbase) + $1
(Namecheap, amortized) = **about $209/mo of pure platform overhead**
before a single OpenRouter token is spent. Your variable cost is the one
that scales with usage: ~$15/week across 2 active users is roughly $32/mo
per active user in LLM spend *at your current, mostly-free-tier usage
levels* -- and that number will climb, not stay flat, once a paid tier
allows meaningfully more browsing and texting per day, since browsing in
particular (multi-step agent loops, each step a model call) is the
expensive action per unit, not the cheap one. I'm giving you numbers below
because you asked me to, but I'd flag plainly: these are a reasonable
starting point, not numbers I can promise pencil out, because you don't
yet have per-plan cost data to check them against -- and this proposal's
own usage table is what would let you get that data honestly within a
month of shipping it.

| Plan | Price | outbound_emails/day | browse_actions/day | number_of_texts/day | connected apps | Positioning |
|---|---|---|---|---|---|---|
| Basic | Free | 10 | 5 | 25 | 2 | Your own numbers -- enough to try Messa for real, tight enough to create upgrade pressure |
| Pro | $12/mo | 50 | 25 | 150 | 6 | Removes the free-tier ceiling for a normal daily user; not "unlimited" |
| Plus | $29/mo suggested | 200 | 75 | 500 | 15 | For someone routing a meaningful chunk of their day through Messa |
| Business | $79/mo suggested | 1000 | 300 | 2000 | Unlimited | Effectively unlimited in practice on daily actions, capped only against abuse; roughly matches your own per-seat Browserbase/Sendblue economics for a power user |

Treat Plus/Business names and numbers as placeholders for you to react to,
not a final call -- you said you still need to figure those two out, and I
don't think anyone should lock in tier 3/4 pricing before real per-plan
cost data exists. "Unlimited" in this table is always the same underlying
value everywhere in the system -- Python `None` in `plans.py`, JSON `null`
in the Hugging Face override, and rendered as the word "Unlimited" (never
a number, never blank) on the pricing page and in anything Messa says.
That's also exactly the mechanism for the messaging tier you mentioned
wanting someday: making `number_of_texts` genuinely infinite for some
future plan is a one-line change from a number to `None` in `plans.py` --
no new concept, no migration, nothing structural has to change to support
it when you're ready.

## Build order

1. This round: `messa/plans.py` (with the Hugging Face JSON override and
   the `max_connected_apps` field), the `plan_id` and `is_admin` migration,
   the `usage_daily_counts` table + `check_and_increment_usage` (admin
   bypass built in from the start, not bolted on later), the five hook
   points above, the SMS chicken-and-egg fix plus the backlog system-note,
   and a `UsageLimiter` object threaded through `build_orchestrator` the
   same way `approval_gate` already is. No Stripe, no payment page yet --
   Messa can already tell a user they're over their limit and that
   upgrading exists, even before there's anywhere to click.
2. Next: the static pricing page + a manual (you-approve-it) plan-upgrade
   path, once plan 3/4 pricing is actually decided.
3. Later ("soon," per you): real Stripe Payment Links per plan and a
   webhook that flips `plan_id` automatically.

## Speed and reliability, specifically

Pulling together the choices above that were made for exactly this
reason: the plans blob is parsed and validated once at boot, never on the
request path, so normal traffic never pays a parsing cost and a bad
config is caught before it can affect a single user. The daily counters
use one atomic SQL statement per check, so there's no read-then-write
race under concurrent tool calls and no separate locking to reason about.
The connected-apps cap reads Composio's live state directly instead of a
cache, so it can't drift out of sync with reality. The text-limit check
runs before the agent loop starts, so a blocked message costs nothing --
no model call, no tool call, near-instant response. And the admin bypass
lives in exactly one function that every other check already calls
through, so there's a single place to verify it's correct rather than a
scattered set of if-checks that could fall out of sync with each other as
the feature grows.

## Testing

Following this project's existing fake-Postgres-pool test pattern (the
same shape as `/tmp/test_dynamic_connected_apps.py`): the day-bucketing
math across a timezone boundary, the atomic check-and-increment under
concurrent calls, each of the five hook points (including that
`execute_integration_tool`'s slug-matching correctly catches
`OUTLOOK_SEND_EMAIL` and correctly ignores an unrelated Outlook action,
and that `max_connected_apps` blocks a new connection at the cap but never
touches an already-connected app), the one-notice-per-day limit-exceeded
behavior plus the backlog system-note actually being written once per
window rather than once per blocked message, `is_admin` bypassing every
check while still incrementing the counters, the Hugging Face JSON
override parsing correctly (including `null` round-tripping to
unlimited) and failing loudly on a malformed blob, and that a plan with
`None` for a feature never gets throttled. Same README-documented-limits
convention as every other feature here: what's verified is that limits are
computed and enforced correctly, not that the LLM phrases the upgrade
nudge well in every conversational context -- that's a live-model
judgment call, same caveat as the rest of this codebase's prompt-driven
behavior.
