"""Central configuration: env loading, model factories, shared constants.

Every other module imports settings from here instead of touching
`os.environ` directly, so switching providers/models is a one-file change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from . import plans

load_dotenv()


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. Add it to your .env file."
        )
    return val


# ---- LLM config ----
# Two separate models now, not one shared by everything:
#
# - ORCHESTRATOR_MODEL_NAME is Messa's own model -- the one that decides
#   whether/when to delegate. This is specifically where the "empty
#   promise" bug (agents/registry.py's "Critical:" paragraph, README's
#   "A delegation Messa announces but never actually makes" section) bites:
#   Messa narrating a delegation in text without actually including the
#   tool call, which the framework treats as a normal, final reply and
#   silently drops the delegation. That failure is about instruction-
#   following/tool-calling discipline specifically, which is what a
#   stronger ("pro"-tier) model buys you -- so per your call, Messa gets
#   the pro model and everyone else stays on the cheaper/faster one.
# - SUBAGENT_MODEL_NAME is used by every subagent: deepsearch (see
#   agents/registry.py's build_orchestrator, which passes it into
#   build_deepsearch_subagent) and the four dict-based subagents
#   (executive_assistant, email_agent, document_agent, routines_agent),
#   which each get `"model": subagent_model` in their spec -- deepagents'
#   own SubAgent dict supports a per-subagent `model` override (see
#   deepagents/middleware/subagents.py's SubAgent.model field); without
#   it, a subagent silently inherits whatever model create_deep_agent's
#   own `model=` argument was called with. Their jobs are narrower
#   (execute one delegated task, not decide whether to delegate at all),
#   so they're less prone to that specific failure mode, and running 5-6
#   agents per turn on the pricier model would multiply cost for little
#   extra reliability where it doesn't matter as much.
#
# The `~` prefix on the flash model string below isn't a documented
# OpenRouter convention I could find -- it's just what was already proven
# working in this project (every deepsearch run so far has gone through
# it), so ORCHESTRATOR_MODEL_NAME's default mirrors the same prefix rather
# than introducing an unverified deviation. I couldn't verify
# "~deepseek/deepseek-v4-pro" against the real OpenRouter API myself (no
# real credentials touch this sandbox, by design -- see this project's own
# testing discipline), so if it 400s on your first real message, the
# quickest fallback is dropping the tilde (`deepseek/deepseek-v4-pro`) or
# pinning the dated snapshot (`deepseek/deepseek-v4-pro-0813`) via
# MESSA_MODEL, no code change needed either way.
OPENROUTER_API_KEY = _require("OPENROUTER_API_KEY")
ORCHESTRATOR_MODEL_NAME = os.environ.get("MESSA_MODEL", "deepseek/deepseek-v4-pro")
SUBAGENT_MODEL_NAME = os.environ.get("MESSA_SUBAGENT_MODEL", "~deepseek/deepseek-v4-flash-latest")

# A pool of OpenRouter API keys deepsearch's concurrent delegate_website_task
# sub-workers round-robin across (see tools/deepsearch_tools.py's
# _delegate_website_task) -- per the CTO discussion on why parallel
# sub-agents weren't producing the wall-clock speedup a naive 3x-concurrency
# estimate would suggest. This does NOT change per-call model latency (same
# model, same provider, same request) -- it exists specifically to spread
# concurrent sub-workers across separate OpenRouter keys/accounts, in case a
# SHARED key's own per-key concurrent-request ceiling (common on cheaper
# tiers) was silently queuing "concurrent" sub-agent LLM calls behind each
# other server-side, which would look exactly like "the code fires them in
# parallel but the clock doesn't move." Comma-separated in
# MESSA_OPENROUTER_API_KEY_POOL; unset (the default) means a pool of exactly
# one -- OPENROUTER_API_KEY itself -- so behavior is unchanged unless you
# actually add more keys. This is a real, if unproven-from-this-sandbox,
# lever: whether it helps depends entirely on whether a shared-key
# concurrency ceiling is actually the bottleneck, which is why the other
# half of this change is the per-sub-worker timing log below (see
# _TimingCallback) -- that's what tells you, from a real run, whether this
# was worth adding at all.
SUBAGENT_API_KEY_POOL = [
    k.strip() for k in os.environ.get("MESSA_OPENROUTER_API_KEY_POOL", "").split(",") if k.strip()
] or [OPENROUTER_API_KEY]

# ---- Per-agent OpenRouter API keys -- a DIFFERENT lever from the pool just
# above. SUBAGENT_API_KEY_POOL spreads deepsearch's own CONCURRENT
# delegate_website_task sub-workers across keys (see _pick_subagent_model in
# deepsearch_tools.py) -- it says nothing about which key Messa's own
# orchestrator uses, or whether deepsearch's TOP-LEVEL model shares a key
# with the other 6 lighter subagents (executive_assistant, email_agent,
# personal_inbox_agent, document_agent, routines_agent, integrations_agent,
# admin_agent). Before this, ALL of those (orchestrator included) defaulted
# to the exact same OPENROUTER_API_KEY, so a rate/concurrency ceiling on
# that one key/account was a single shared bottleneck across literally
# every agent in the app -- including Messa's own reply to the user, which
# is the one that actually needs to feel fast.
#
# This splits that into three independently-configurable keys, by traffic
# pattern (per the actual product priority -- see registry.py's "Speed
# matters" paragraph):
#   1. Messa's own orchestrator -- touches every single message, the most
#      latency-sensitive agent by far.
#   2. deepsearch -- the heaviest single subagent (long browsing runs,
#      already has its own internal concurrency via the pool above).
#   3. Everything else -- occasional, bursty, much lighter traffic; fine to
#      share one key among all 6.
# Each is optional and falls back to OPENROUTER_API_KEY unset -- so setting
# NONE of these changes nothing about today's behavior.
ORCHESTRATOR_API_KEY = os.environ.get("MESSA_ORCHESTRATOR_API_KEY") or OPENROUTER_API_KEY
DEEPSEARCH_API_KEY = os.environ.get("MESSA_DEEPSEARCH_API_KEY") or OPENROUTER_API_KEY
SUBAGENT_API_KEY = os.environ.get("MESSA_SUBAGENT_API_KEY") or OPENROUTER_API_KEY

# Fine-grained escape hatch on top of the three-tier split above: pull ANY
# individual subagent onto its own key (e.g. once you have a 4th or 5th key
# and want email_agent split off from the shared "everything else" group
# too), with no code change. Format: "agent_name=key,agent_name=key" --
# comma-separated pairs, agent name exactly matching its subagent "name"
# field (e.g. "deepsearch", "email_agent", "executive_assistant"). An
# unrecognized agent name here is simply never looked up (see
# api_key_for_agent below) -- harmless, not an error, so a typo doesn't
# break startup.
def _parse_agent_key_overrides(raw: str) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, key = pair.partition("=")
        name, key = name.strip(), key.strip()
        if name and key:
            overrides[name] = key
    return overrides


AGENT_API_KEY_OVERRIDES: dict[str, str] = _parse_agent_key_overrides(
    os.environ.get("MESSA_AGENT_API_KEY_OVERRIDES", "")
)


def api_key_for_agent(agent_name: str) -> str:
    """Resolves which OpenRouter key a given SUBAGENT (not the orchestrator
    -- that's always ORCHESTRATOR_API_KEY, resolved directly where the
    orchestrator model is built) should use, in priority order:
    1. AGENT_API_KEY_OVERRIDES[agent_name], if that specific agent was
       pulled out onto its own key.
    2. DEEPSEARCH_API_KEY, if this is deepsearch specifically.
    3. SUBAGENT_API_KEY, the shared default for every other subagent.
    All three ultimately fall back to OPENROUTER_API_KEY if nothing at all
    is configured -- see each var's own definition above."""
    if agent_name in AGENT_API_KEY_OVERRIDES:
        return AGENT_API_KEY_OVERRIDES[agent_name]
    if agent_name == "deepsearch":
        return DEEPSEARCH_API_KEY
    return SUBAGENT_API_KEY

# Backward-compatible alias: kept in case anything (or you) still refers to
# "the main model" -- always Messa's own orchestrator model.
MAIN_MODEL_NAME = ORCHESTRATOR_MODEL_NAME

# ---- Token-cost control: how eagerly each agent's history gets compacted ----
# deepagents' create_deep_agent() always attaches a SummarizationMiddleware
# to every agent it builds (main + every subagent) -- it isn't optional, and
# by design we never asked for it explicitly, so its trigger/keep thresholds
# come entirely from deepagents.middleware.summarization
# .compute_summarization_defaults(model). That function looks at
# `model.profile["max_input_tokens"]` to decide HOW to pick thresholds: if
# present, it uses fractions of it (85% full -> summarize, keep last 10%);
# if absent, it falls back to a fixed, provider-agnostic default of
# trigger=170,000 raw tokens / keep=last 6 messages.
#
# Confirmed empirically (not documented behavior I'm guessing at): a
# ChatOpenAI instance built by build_model() below has `.profile is None`,
# because LangChain's model-profile lookup only recognizes OpenAI's own
# published model names -- "~deepseek/deepseek-v4-pro" routed through
# OpenRouter matches nothing in that table. So every agent in this project
# -- Messa herself and all five subagents -- has been running on the 170k-
# token fallback the entire time, completely independent of how any of
# their system prompts are worded.
#
# Why that number matters more than it sounds like it should: a chat-
# completions-style agent loop resends its ENTIRE message history on every
# single step (there's no server-side conversation state to lean on here),
# so total tokens billed across an N-step run is dominated by that growing
# history, not by the comparatively fixed per-step cost of the system
# prompt/tool schemas -- it's roughly quadratic in N, not linear. deepsearch
# specifically runs up to DEEPSEARCH_MAX_STEPS=100 steps and appends a fresh
# browser_snapshot (often several thousand tokens on a busy page -- see the
# README's "Making deepsearch faster" section) on most of them, so it's the
# agent most likely to be quietly compounding cost for a long time before
# 170k is ever reached, if it's reached at all before the run ends.
#
# `.profile` is a plain, writable attribute on a BaseChatModel instance --
# LangChain only ever POPULATES it from that static lookup table, it never
# protects it from being overwritten afterward (confirmed by just setting
# it). Stamping a deliberately modest, hand-chosen budget onto it here has
# NOTHING to do with the model's real context window (OpenRouter/the actual
# model enforces that on its own, oblivious to whatever we write into a
# LangChain-side Python attribute) -- it exists purely to tell deepagents'
# summarization heuristic "start being defensive well before you'd
# otherwise think to," so long-running tool-heavy work gets compacted in
# regular, bounded increments instead of ballooning unchecked for dozens of
# steps and then taking one enormous, late summarization hit (or, on a run
# that finishes before 170k, never compacting at all).
#
# Two different budgets, not one shared value, because the two use cases
# have different quality trade-offs: SUBAGENT_EFFECTIVE_CONTEXT_TOKENS
# governs deepsearch (top-level and every delegate_website_task sub-worker)
# and the other four subagents -- each one is a bounded, single-task
# delegation where old tool-call noise from early in that task is rarely
# worth paying to keep around, so an aggressive budget is a clear win.
# ORCHESTRATOR_EFFECTIVE_CONTEXT_TOKENS governs Messa's own ongoing
# conversation with the user, which is closer to a persistent relationship
# than a bounded task -- compacting that too aggressively risks losing
# "what we talked about a few days ago" continuity for comparatively little
# savings (a text-message conversation is nowhere near as token-heavy per
# turn as a tool-call-and-snapshot-heavy browsing run), so it gets a larger,
# more conservative budget: real savings over the 170k fallback, without
# meaningfully changing how much conversational history Messa keeps handy.
# Both are overridable via env var without a code change, same pattern as
# every other tunable in this file.
SUBAGENT_EFFECTIVE_CONTEXT_TOKENS = int(
    os.environ.get("MESSA_SUBAGENT_EFFECTIVE_CONTEXT_TOKENS", "60000")
)
ORCHESTRATOR_EFFECTIVE_CONTEXT_TOKENS = int(
    os.environ.get("MESSA_ORCHESTRATOR_EFFECTIVE_CONTEXT_TOKENS", "120000")
)


def build_model(
    model_name: str = ORCHESTRATOR_MODEL_NAME,
    *,
    effective_context_tokens: int | None = None,
    api_key: str | None = None,
) -> ChatOpenAI:
    """Build a ChatOpenAI client pointed at OpenRouter.

    effective_context_tokens, when given, is stamped onto the returned
    model's `.profile` as `{"max_input_tokens": ...}` -- see the big
    comment above SUBAGENT_EFFECTIVE_CONTEXT_TOKENS/
    ORCHESTRATOR_EFFECTIVE_CONTEXT_TOKENS for why this exists and what it
    actually controls (deepagents' summarization-compaction thresholds,
    NOT the model's real context window). Left unset (the default), the
    returned model behaves exactly as before this was added -- `.profile`
    stays `None` and deepagents falls back to its own generic default.

    api_key, when given, overrides OPENROUTER_API_KEY -- see
    SUBAGENT_API_KEY_POOL's own comment for why (spreading concurrent
    delegate_website_task sub-workers across separate OpenRouter keys).
    Left unset (the default), behavior is unchanged from before this was
    added.
    """
    model = ChatOpenAI(
        model=model_name,
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key or OPENROUTER_API_KEY,
        # Scalability fix (pre-1000-user launch): unset before this, so a
        # hung/slow OpenRouter response could stall a user's whole turn
        # (and the asyncio task handling their webhook) for however long
        # the underlying httpx client's own default takes to give up --
        # effectively unbounded from this app's point of view. This is a
        # safety-net ceiling, not a normal-path limit: a real call
        # finishes in single-digit seconds per the timing logs threaded
        # through this project's own console.system lines, so timing out
        # at LLM_REQUEST_TIMEOUT_SECONDS only ever fires on a genuinely
        # stuck request, and just turns that into a normal caught
        # exception (server.py's existing "something went wrong, mind
        # trying again" fallback) instead of an indefinitely hung task
        # quietly eating a slot every user is sharing.
        timeout=LLM_REQUEST_TIMEOUT_SECONDS,
    )
    if effective_context_tokens is not None:
        model.profile = {"max_input_tokens": effective_context_tokens}
    return model


# How long a single OpenRouter call is allowed to hang before this app gives
# up on it -- see build_model's own comment for the full "why". 120s is
# generous relative to real observed call latency (single-digit seconds
# typically, low double digits for a heavily-loaded deepsearch planning
# call) so this should never fire on a healthy request, only a genuinely
# stuck one.
LLM_REQUEST_TIMEOUT_SECONDS = int(os.environ.get("MESSA_LLM_REQUEST_TIMEOUT_SECONDS", "120"))

# ---- Database ----
# SQLAlchemy-style DSN in .env (postgresql+asyncpg://...). asyncpg wants a
# plain postgresql:// DSN, so we strip the driver qualifier here.
_RAW_DATABASE_URL = _require("DATABASE_URL")
DATABASE_URL = _RAW_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

# Scalability fix (pre-1000-user launch): the pool used to be hardcoded at
# min_size=1, max_size=5 in db.py -- fine for a handful of test users, a
# real bottleneck once you have N concurrent webhook turns PLUS ~9 always-
# on background poller loops all sharing the same 5 connections. Both ends
# are env-configurable now (db.py's get_pool reads these) so this can be
# tuned per deploy without a code change. 20 is a reasonable default for a
# single-container launch -- this is still going through Neon's PgBouncer
# pooled endpoint (see db.py's own statement_cache_size=0 comment for why
# that matters), which multiplexes many more client-side connections than
# this onto a much smaller number of real Postgres backend connections, so
# raising this number is safe regardless of your actual Neon plan tier.
DB_POOL_MIN_SIZE = int(os.environ.get("MESSA_DB_POOL_MIN_SIZE", "2"))
DB_POOL_MAX_SIZE = int(os.environ.get("MESSA_DB_POOL_MAX_SIZE", "20"))
# Per-query ceiling (asyncpg's own `command_timeout`, applied to every
# query on every connection in the pool automatically -- no per-call-site
# changes needed). Same "safety net, not a normal-path limit" reasoning as
# LLM_REQUEST_TIMEOUT_SECONDS above: a healthy query returns in
# milliseconds, so this only ever fires on something genuinely stuck
# (Neon blip, a lock some other transaction is holding), turning an
# indefinite hang into a normal caught exception instead of a connection
# (and whoever's waiting on it) stuck forever.
DB_COMMAND_TIMEOUT_SECONDS = int(os.environ.get("MESSA_DB_COMMAND_TIMEOUT_SECONDS", "30"))

# ---- Composio (email agent) ----
COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY")  # optional until Phase 2 wiring

# Real multi-tenant OAuth now (each user connects their own Gmail): the
# earlier COMPOSIO_EMAIL_CONNECTED_ACCOUNT_ID single-shared-account env var
# is gone -- every Composio call is keyed by that specific user's own id
# (see email_tools.py's _composio_user_id), and the connected account it
# resolves to is whichever one that user completed the OAuth flow for.
#
# COMPOSIO_GMAIL_AUTH_CONFIG_ID is optional: leave it unset and
# email_tools.py finds-or-creates one Composio-managed Gmail auth config
# automatically (scoped, via tool_access_config, to exactly the four Gmail
# actions this project calls -- read/search/get/send/reply -- so Composio
# computes the minimum OAuth scopes for those, not a blanket full-account
# grant). That auto-created config is found again (by name) on every cold
# start rather than recreated, so this only needs setting explicitly if you
# want to point at your own custom Gmail OAuth app instead of Composio's
# managed one.
COMPOSIO_GMAIL_AUTH_CONFIG_ID = os.environ.get("COMPOSIO_GMAIL_AUTH_CONFIG_ID")
COMPOSIO_TOOLKIT_VERSION = os.environ.get("COMPOSIO_TOOLKIT_VERSION")

# Where Composio sends the user after they finish (or abandon) the OAuth
# consent screen. Optional -- omit it and Composio shows its own generic
# "connected" page; there's no callback endpoint of ours to host either
# way, since connection status is confirmed by polling Composio's API
# (see _production_email_connection_poll_loop in server.py), not by us
# receiving a callback request.
COMPOSIO_GMAIL_CALLBACK_URL = os.environ.get("COMPOSIO_GMAIL_CALLBACK_URL")

# How often the background loop checks pending Gmail connection requests
# for whether the user has finished the OAuth flow yet. Not time-critical
# the way the deepsearch human-help pause is (nothing is blocked waiting on
# it mid-conversation), so a slower cadence than that loop's 5s is fine.
EMAIL_CONNECTION_POLL_INTERVAL_SECONDS = int(
    os.environ.get("MESSA_EMAIL_CONNECTION_POLL_INTERVAL_SECONDS", "20")
)

# A connection request nobody ever finished (closed the tab, changed their
# mind) shouldn't be polled forever -- bounds both the poll loop's Composio
# API usage and the table's growth. Expiring is silent (no "did you still
# want to connect?" text); the user can always just ask again.
EMAIL_CONNECTION_REQUEST_EXPIRES_HOURS = int(
    os.environ.get("MESSA_EMAIL_CONNECTION_REQUEST_EXPIRES_HOURS", "24")
)

# ---- Dynamic Integration Engine (tools/integration_tools.py, migrations/
# 020_dynamic_integrations.sql) -- Composio's 1,400+ app catalog, generic
# and per-user, on top of (not replacing) the Gmail-specific block above.
# Reuses COMPOSIO_API_KEY/COMPOSIO_TOOLKIT_VERSION as-is; everything below
# is new and specific to the generic path.

# How many candidate tools search_integration_tools hands back to the model
# per call -- kept small on purpose (this is the whole point of the
# feature: inject a handful of matching tool schemas, not a whole
# toolkit's worth) but Composio's own tools.get(search=...) is documented
# as "(experimental)", so this stays a knob rather than a hardcoded value in
# case real usage shows the default needs tuning.
#
# Raised from 5 to 8 after a real production run (fetching Reddit posts,
# see README) showed 5 was occasionally too tight for a query that hadn't
# been scoped to a specific toolkit yet -- an UNSCOPED single-word query
# like "reddit" can rank several other toolkits' actions (whose
# descriptions merely mention "Reddit") ahead of the one actually wanted.
# The real fix for that is scoping the search with search_integration_tools'
# new `toolkit` parameter once the app is known (see that tool's own
# docstring) -- this bump is just a modest, cheap safety margin on top,
# not a substitute for scoping. Deliberately NOT raised further (e.g. to
# 15-20): once a search is toolkit-scoped there are usually only a
# handful of real candidates anyway, and a much larger limit mostly just
# adds token cost from irrelevant results on the (increasingly rare)
# unscoped search.
INTEGRATION_SEARCH_RESULT_LIMIT = int(
    os.environ.get("MESSA_INTEGRATION_SEARCH_RESULT_LIMIT", "8")
)

# Same role as EMAIL_CONNECTION_POLL_INTERVAL_SECONDS/_REQUEST_EXPIRES_HOURS
# above, generalized to any toolkit instead of just Gmail -- see
# server.py's _production_app_connection_poll_loop and
# db.py's app_connection_requests functions.
APP_CONNECTION_POLL_INTERVAL_SECONDS = int(
    os.environ.get("MESSA_APP_CONNECTION_POLL_INTERVAL_SECONDS", "20")
)
APP_CONNECTION_REQUEST_EXPIRES_HOURS = int(
    os.environ.get("MESSA_APP_CONNECTION_REQUEST_EXPIRES_HOURS", "24")
)

# Deliberately its own setting, not a reuse of COMPOSIO_GMAIL_CALLBACK_URL --
# that one's name is specific to the Gmail flow it was written for, and
# leaving them separate means either can be set (or left unset, same
# graceful "Composio shows its own generic page" degrade as Gmail's) without
# implying anything about the other.
COMPOSIO_CALLBACK_URL = os.environ.get("COMPOSIO_CALLBACK_URL")

# Composio action slugs are consistently named TOOLKIT_VERB_NOUN (e.g.
# SLACK_POST_MESSAGE, NOTION_DELETE_PAGE, TODOIST_CREATE_TASK,
# GMAIL_SEND_EMAIL -- see toolkits.composio.dev). execute_integration_tool
# treats a slug as a write/state-changing action -- and therefore routes it
# through the same ApprovalGate mechanism send_email/reply_to_email already
# use -- if any of these verbs appears in it. A slug matching none of these
# (e.g. a *_LIST_*/*_GET_*/*_FETCH_*/*_SEARCH_* read) executes immediately.
# Deliberately a keyword heuristic over Composio's own naming convention
# rather than a hand-maintained per-toolkit table (1,400+ toolkits, growing
# continuously) -- err on the side of gating: a slug this misses as
# "destructive" just means an unnecessary confirmation prompt, not a
# skipped one, since anything genuinely unrecognized... see
# tools/integration_tools.py's own docstring for the exact fallback rule.
INTEGRATION_WRITE_ACTION_KEYWORDS = (
    "CREATE", "UPDATE", "DELETE", "REMOVE", "POST", "SEND", "PUBLISH",
    "TRANSFER", "PAY", "INVITE", "ADD", "EDIT", "ARCHIVE", "CANCEL",
    "SHARE", "UPLOAD", "MOVE", "REPLY", "COMMENT", "ASSIGN", "CLOSE",
    "MERGE", "APPROVE", "REJECT", "SUBMIT", "SCHEDULE",
)

# ---- Site credentials (messa/credentials.py, migrations/021_site_credentials.sql)
# ---- account passwords Messa creates/stores for sites outside Composio's
# catalog (see tools/deepsearch_tools.py's generate_account_credential/
# get_account_credential). Optional until you're ready to turn this on:
# with CREDENTIALS_ENCRYPTION_KEY unset, both tools refuse plainly instead
# of ever generating a password with nothing safe to encrypt it with.
#
# Generate one with:
#   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Treat it exactly like a database password -- anyone with this key AND a
# copy of the site_credentials table can decrypt every stored password.
# Losing it means every already-stored credential becomes permanently
# unrecoverable (the accounts still exist on their real sites -- only
# Messa's own copy of the password is lost); rotating it requires
# decrypting everything under the old key and re-encrypting under the new
# one first (no built-in migration tool for that in this build).
CREDENTIALS_ENCRYPTION_KEY = os.environ.get("MESSA_CREDENTIALS_ENCRYPTION_KEY")

# ---- Messa's own personal-inbox email (separate from the Gmail block above)
# ----
# <local-part>@TEXTMESSA_EMAIL_DOMAIN is an address Messa owns outright for
# each user -- no OAuth, nothing borrowed from the user's real inbox. Inbound
# is free (Cloudflare Email Routing -> a small Worker in
# cloudflare/personal-email-worker/ -> server.py's
# /webhooks/personal-email/inbound); outbound goes through Resend (also has
# a free tier) -- see channels/resend.py and tools/personal_inbox_tools.py.
# Both env vars are optional until you're ready to turn this on: with
# RESEND_API_KEY unset, personal_inbox_tools.py's send/reply tools just say
# so instead of pretending to work, same pattern as COMPOSIO_API_KEY above.
TEXTMESSA_EMAIL_DOMAIN = os.environ.get("MESSA_TEXTMESSA_EMAIL_DOMAIN", "textmessa.com")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

# ---- Contact info for the public Privacy Policy page (GET /privacy, see
# privacy_page.py) ----
# Messa is currently operated as a sole proprietorship, not a registered
# company -- there's no separate "legal entity" to name, and deliberately
# no personal name or home address published on that page either (by
# request). Email is the only contact info this page discloses.
PRIVACY_CONTACT_EMAIL = os.environ.get("MESSA_PRIVACY_CONTACT_EMAIL", f"privacy@{TEXTMESSA_EMAIL_DOMAIN}")

# Cap on a PDF (or any other file) attached to an outbound Messa-email send/
# reply (tools/document_tools.py's generate_pdf -> tools/personal_inbox_tools.py's
# send_email/reply_to_email -> channels/resend.py). Resend's own hard limit is
# 40MB for the whole request AFTER base64 encoding (~33% larger than the raw
# file) plus headers/body/JSON overhead -- this is checked against the RAW
# file size before encoding, so it's set well under that ceiling (25MB raw
# -> ~33MB encoded) to leave comfortable room rather than cutting it close.
MAX_EMAIL_ATTACHMENT_BYTES = int(
    os.environ.get("MESSA_MAX_EMAIL_ATTACHMENT_BYTES", str(25 * 1024 * 1024))
)

# Same idea, sized for an outbound MMS/iMessage attachment instead
# (tools sending via channels/sendblue.py's media_url -- see
# db.create_document_share / server.py's GET /files/{token}). Deliberately
# much smaller than MAX_EMAIL_ATTACHMENT_BYTES: unlike email, an MMS
# recipient on the SMS/RCS fallback (not iMessage over data) is often
# behind a real carrier size cap of just a few MB, and Sendblue's own CDN
# re-hosting doesn't change what the receiving carrier will actually
# deliver. 8MB comfortably covers a real generated PDF (mostly text) while
# staying well under typical carrier MMS ceilings.
MAX_SMS_ATTACHMENT_BYTES = int(
    os.environ.get("MESSA_MAX_SMS_ATTACHMENT_BYTES", str(8 * 1024 * 1024))
)

# ---- PDF reading (NOT generation -- document_tools.py's generate_pdf stays
# unlimited) ----
# Applies everywhere Messa reads text out of an existing PDF someone sent
# her: a PDF texted to her (server.py's sendblue_webhook -> pdf_reader.py),
# a PDF attached to inbound mail on her own address (server.py's
# personal_email_inbound_webhook -> pdf_reader.py). Explicit product
# requirement: "keep a pdf limit to only read (not write), which can be
# easily modified by a variable when needed" -- this is that variable.
MAX_PDF_READ_PAGES = int(os.environ.get("MESSA_MAX_PDF_READ_PAGES", "10"))

# A separate cap on the raw file size before any parsing is even attempted
# -- a hostile or just enormous PDF shouldn't be downloaded/decoded at all,
# regardless of how many pages MAX_PDF_READ_PAGES would eventually stop at.
# Sized a bit under MAX_EMAIL_ATTACHMENT_BYTES: reading in is cheaper to
# cap tightly than sending out is to cap loosely.
MAX_PDF_READ_BYTES = int(os.environ.get("MESSA_MAX_PDF_READ_BYTES", str(20 * 1024 * 1024)))

# Shared secret the Cloudflare Worker sends as the X-Messa-Webhook-Secret
# header on every inbound-email POST -- same role as SENDBLUE_WEBHOOK_SECRET
# below, and same tradeoff: unset means the endpoint accepts any request
# with no check (fine for local/dev, not for a real deployment). Set with
# `wrangler secret put MESSA_WEBHOOK_SECRET` on the Worker side and the
# matching value here.
PERSONAL_EMAIL_WEBHOOK_SECRET = os.environ.get("MESSA_PERSONAL_EMAIL_WEBHOOK_SECRET")

# A hostile/huge inbound email (or just a very long newsletter someone
# forwarded in) shouldn't blow up the synthetic prompt server.py builds for
# it -- truncated defensively before it ever reaches the model.
INBOUND_EMAIL_BODY_MAX_CHARS = int(os.environ.get("MESSA_INBOUND_EMAIL_BODY_MAX_CHARS", "4000"))

# ---- Browserbase (deepsearch's remote browser) ----
# Two rounds of "Chrome isn't installed" on the HF Space -- each one a real,
# fixable bug (MCP not passing through the environment; a --browser channel
# mismatch), but both symptoms of the same underlying problem: running an
# actual Chromium inside a constrained, ephemeral container. Browserbase
# runs the browser on its own infrastructure instead; deepsearch just
# drives it over CDP (see tools/deepsearch_tools.py). Required for
# deepsearch to work at all now -- there's no local-Chromium fallback.
BROWSERBASE_API_KEY = os.environ.get("BROWSERBASE_API_KEY")

# Explicit session timeout (seconds), passed on every browserbase.create_session
# call. Root-caused a real bug: whole deepsearch sessions -- including ones
# that never touched request_human_help at all -- were cutting off at
# exactly 5 minutes. DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS (also 300s) was
# the obvious suspect but is scoped correctly (it only ever ends ONE tab's
# wait, never the whole run -- see request_human_help), and
# DEEPSEARCH_MAX_SESSION_SECONDS is 1200s, not 300s. The actual cause: we
# never passed Browserbase's own `timeout` param on session creation, so it
# silently fell back to "the Project's defaultTimeout" (confirmed via
# https://docs.browserbase.com/reference/api/create-a-session) -- a
# dashboard setting outside this code, evidently defaulting low (5 minutes)
# on this project. Passing it explicitly here removes that dependency on an
# out-of-repo dashboard setting entirely. Sized comfortably above
# DEEPSEARCH_MAX_SESSION_SECONDS (our own belt-and-suspenders cap, which
# fires first in the normal case) so this is a generous backstop, not the
# thing actually bounding a session's length day to day. Browserbase accepts
# 60-21600 seconds (max 6 hours); this is well inside that range.
BROWSERBASE_SESSION_TIMEOUT_SECONDS = int(
    os.environ.get("MESSA_BROWSERBASE_SESSION_TIMEOUT_SECONDS", "1800")
)

# ---- "Faster Than Human" branch: low-risk deepsearch speed/robustness
# upgrades that don't depend on which browser-automation engine drives the
# page (Playwright MCP today, possibly Stagehand later -- see
# faster-than-human.md). Each is independently toggleable so a live-test
# regression can isolate which one, if any, is the cause.

# Browserbase's own `browserSettings.blockAds` (a plain boolean the session-
# create call passes straight through -- see channels/browserbase.py) strips
# third-party ad/tracker network requests before they ever load, which is
# both a real page-load speedup and a cleaner Live View for the user
# watching. No uBlock/EasyList list-management needed on our side -- this is
# a native Browserbase feature, not something we're building. Confirmed in
# Browserbase's own API reference (docs.browserbase.com/reference/api/
# create-a-session); default true since there's no known cost to it (it
# doesn't block same-origin app functionality, only recognized third-party
# ad/tracker domains) -- flip off per-deploy only if a live test finds a
# site whose actual login/checkout flow depends on a blocked domain.
# Deepsearch automation engine: "stagehand" (Stagehand v4 macro-actions via WebMCP)
# or "playwright" (@playwright/mcp micro-actions). Defaults to "stagehand" on feature/stagehand-v4.
DEEPSEARCH_ENGINE = os.environ.get("MESSA_DEEPSEARCH_ENGINE", "stagehand").strip().lower()

# Model for Stagehand in-browser reasoning (empty string uses Browserbase model gateway auto-routing)
STAGEHAND_MODEL = os.environ.get("MESSA_STAGEHAND_MODEL", "").strip()

DEEPSEARCH_BLOCK_ADS = os.environ.get("MESSA_DEEPSEARCH_BLOCK_ADS", "true").strip().lower() in (
    "1", "true", "yes", "on",
)

# Auto-dismiss cookie/consent banners the moment they mount, via a small
# heuristic script injected through @playwright/mcp's --init-script flag
# (same mechanism cursor_overlay.js already uses -- see
# tools/deepsearch_tools.py's _spawn_mcp_http_server). Deliberately
# conservative: only clicks elements whose visible text matches a known
# cookie-consent vocabulary ("accept all", "i agree", etc.) inside something
# that looks like a banner/dialog, never a bare "accept"/"x" anywhere on the
# page -- a false positive here (clicking the wrong thing) is worse than
# leaving a real banner up for the agent to handle the normal way. See
# assets/deepsearch_enhancements.js for the actual matching logic and its
# own safety notes. Default true; the live-test branch is exactly what
# should catch a site where this heuristic misfires.
DEEPSEARCH_AUTO_DISMISS_CONSENT = os.environ.get(
    "MESSA_DEEPSEARCH_AUTO_DISMISS_CONSENT", "true"
).strip().lower() in ("1", "true", "yes", "on")

# Per-USER concurrent top-level deepsearch sessions, layered UNDER (not
# instead of) DEEPSEARCH_MAX_CONCURRENT_SESSIONS just below, which only caps
# the process-wide total across every user. Without this, a single user
# firing off several browsing requests in a row can occupy a large chunk of
# the whole container's global cap by themselves, starving everyone else --
# exactly the "50 concurrent users, cost controls" scenario faster-than-
# human.md calls out. Enforced in tools/deepsearch_tools.py's
# build_deepsearch_subagent._run via deepsearch_control.active_count(), which
# already exists (built for the "cancel my active search" feature) and
# happens to be exactly the count this needs too -- no new tracking
# structure required. 2 matches the doc's own suggested default: high enough
# that "look this up, then also check that" (two legitimate concurrent asks)
# isn't blocked, low enough that one user can't monopolize the global cap.
DEEPSEARCH_MAX_CONCURRENT_SESSIONS_PER_USER = int(
    os.environ.get("MESSA_DEEPSEARCH_MAX_CONCURRENT_SESSIONS_PER_USER", "2")
)

# ---- Jina Reader (tools/jina_reader.py) -- a JS-capable page read that
# costs no Browserbase session time, sitting between a plain HTTP fetch
# (web_search_tools.fetch_page_text, no JS at all) and a real deepsearch
# delegation (the only tier that can actually click/type/log in, billed by
# Browserbase session TIME). See jina_reader.py's own docstring for the full
# three-tier design and README's "Deepsearch cost tiering" section for why.
#
# Keyless by default -- Jina's own published free tier is 20 requests/minute
# with no key at all, which is exactly why this starts here rather than
# requiring a signup before it does anything. JINA_API_KEY stays unset
# (None) until you add one; nothing else needs to change to start using a
# key later; see README for how a key raises the rate limit once real usage
# outgrows the free tier.
JINA_API_KEY = os.environ.get("MESSA_JINA_API_KEY") or None
JINA_READER_BASE_URL = os.environ.get("MESSA_JINA_READER_BASE_URL", "https://r.jina.ai").rstrip("/")
# JS rendering genuinely takes longer than a plain HTTP GET (fetch_page_text's
# own FETCH_TIMEOUT_SECONDS is 10s) -- sized to give a real JS-heavy page
# room to finish rendering server-side on Jina's infrastructure without
# waiting so long it stops being the "fast" tier in practice.
JINA_READER_TIMEOUT_SECONDS = float(os.environ.get("MESSA_JINA_READER_TIMEOUT_SECONDS", "20"))

# ---- Parallel Search MCP (tools/parallel_search.py) -- a second, backup
# provider at the SAME free/fast tier as Jina Reader and DuckDuckGo, not a
# replacement for either. A remote, hosted MCP server (no local process to
# spawn, unlike @playwright/mcp) exposing two tools: `web_search` (an agent-
# tuned search API, often better-ranked than plain DuckDuckGo) and
# `web_fetch` (JS-rendered/PDF-capable page reads, the same job as Jina
# Reader, from a different provider -- if one of the two is ever down or
# rate-limited, the other is a real fallback). Free and keyless by default,
# same story as Jina: the MCP endpoint itself needs no account or key for
# light use; PARALLEL_API_KEY stays unset until real usage outgrows that.
# See parallel_search.py's own docstring for the full design and README's
# "Deepsearch cost tiering" section for why a SECOND provider at this tier
# is worth having at all.
PARALLEL_MCP_URL = os.environ.get("MESSA_PARALLEL_MCP_URL", "https://search.parallel.ai/mcp")
PARALLEL_API_KEY = os.environ.get("MESSA_PARALLEL_API_KEY") or None
# A remote MCP call is a real network round trip (session init + the tool
# call itself) on top of whatever Parallel's own search/fetch takes -- sized
# well under DEEPSEARCH's own tiers so a slow/unresponsive MCP endpoint
# fails fast and visibly instead of quietly stalling Messa's reply.
PARALLEL_MCP_TIMEOUT_SECONDS = float(os.environ.get("MESSA_PARALLEL_MCP_TIMEOUT_SECONDS", "15"))

# ---- Multi-Provider Web Search & Scraping Waterfall (tools/search_engine.py) ----
# Resilient, fast HTTP search and extraction across legitimate free-tier providers
# (Tavily, Brave Search, Serper/Google, Firecrawl Cloud, Jina Reader).
# Eliminates Browserbase cloud browser overhead for read-only lookups and research.
TAVILY_API_KEY = os.environ.get("MESSA_TAVILY_API_KEY") or os.environ.get("TAVILY_API_KEY") or None
BRAVE_SEARCH_API_KEY = (
    os.environ.get("MESSA_BRAVE_SEARCH_API_KEY") or os.environ.get("BRAVE_SEARCH_API_KEY") or None
)
SERPER_API_KEY = os.environ.get("MESSA_SERPER_API_KEY") or os.environ.get("SERPER_API_KEY") or None
FIRECRAWL_API_KEY = (
    os.environ.get("MESSA_FIRECRAWL_API_KEY") or os.environ.get("FIRECRAWL_API_KEY") or None
)
FIRECRAWL_BASE_URL = os.environ.get("MESSA_FIRECRAWL_BASE_URL", "https://api.firecrawl.dev").rstrip("/")
SEARCH_HTTP_TIMEOUT_SECONDS = float(os.environ.get("MESSA_SEARCH_HTTP_TIMEOUT_SECONDS", "10.0"))


# ---- Memory (messa/memory.py, migrations/027_memory.sql) -- Mem0 on top of
# Postgres/pgvector for a two-tier memory: a cheap always-on profile digest
# (users.memory_profile, read every turn through the app's normal asyncpg
# pool, no Mem0 involved) plus a larger day-by-day episodic archive Mem0
# itself stores, searched only on demand via the recall_past_conversation
# tool and written only by the once-a-day batch job -- never on the live
# per-message hot path. See README's "Memory" section for the full design
# and why.
#
# MESSA_MEM0_DIRECT_DATABASE_URL is deliberately a SEPARATE var from
# DATABASE_URL above, and it MUST be Neon's DIRECT/unpooled connection
# string, not the pooled one DATABASE_URL already uses. Why: the RLS
# backstop below depends on a Postgres session variable (app.current_
# user_id) set once per connection staying in effect for every query that
# follows on that SAME physical connection -- true for a real direct
# connection, NOT guaranteed under PgBouncer's transaction-pooling mode
# (confirmed against Neon's own pooling behavior, and it's exactly why
# DATABASE_URL already needs asyncpg's statement_cache_size=0 workaround
# just above -- same underlying pooling mode). Left unset, the whole memory
# feature no-ops cleanly (messa/memory.py returns a plain "memory isn't set
# up yet" result instead of ever crashing a turn) -- nothing else in this
# app requires it.
MESSA_MEM0_DIRECT_DATABASE_URL = (
    os.environ.get("MESSA_MEM0_DIRECT_DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://") or None
)
MEM0_ENABLED = MESSA_MEM0_DIRECT_DATABASE_URL is not None
# Table name -- matches migrations/027_memory.sql's mem0_memories table
# exactly (Mem0's own auto-create default is just "mem0"; this project
# pre-creates the table itself under this name so the RLS policy in that
# migration is reviewable up front rather than bolted on after Mem0
# auto-creates its own table on first use).
MEM0_COLLECTION_NAME = os.environ.get("MESSA_MEM0_COLLECTION_NAME", "mem0_memories")
# The dedicated per-call connection pool messa/memory.py opens is always
# min=1/max=1 (one physical connection, one user, closed right after) --
# see messa/memory.py's own docstring for why that's the whole point, not
# a limitation to raise.
MEM0_EMBEDDING_DIMS = int(os.environ.get("MESSA_MEM0_EMBEDDING_DIMS", "1536"))
# Which embedder Mem0 uses. "openrouter" (default) reuses OPENROUTER_API_KEY
# with zero new dependency -- unverified live as of this writing (see
# README). "fastembed" is the local/CPU-only fallback (no torch) if that
# doesn't pan out on the real deployment; switching requires also updating
# MESSA_MEM0_EMBEDDING_DIMS to match that model's real output size.
MEM0_EMBEDDER_PROVIDER = os.environ.get("MESSA_MEM0_EMBEDDER_PROVIDER", "openrouter")
# How often server.py's _production_memory_batch_loop checks whether
# today's batch has run yet. Coarse on purpose (default hourly, not the
# tight ~10-20s cadence of the other production pollers -- see
# background.POLL_INTERVAL_SECONDS) -- db.claim_daily_memory_batch_run's
# ON CONFLICT DO NOTHING is what actually enforces "once a day", this
# interval just controls how promptly a missed/restarted day gets picked
# back up.
MEMORY_BATCH_POLL_INTERVAL_SECONDS = float(os.environ.get("MESSA_MEMORY_BATCH_POLL_INTERVAL_SECONDS", "3600"))

# ---- Live view sharing (Phase 3) ----
# Base URL for the public /live/<token> page (see server.py + db.py's
# live_share_token functions). Defaults to the now-permanent custom domain
# rather than an empty string -- an earlier version left this unset by
# default specifically so a missing/misconfigured HF secret would silently
# omit the live link rather than send a broken one, but that meant a
# genuinely-missing MESSA_LIVE_VIEW_BASE_URL secret on the Space was
# indistinguishable from "not ready yet," and cost a live-view link that
# should have gone out. Since live.textmessa.com is stable now, hardcoding
# it as the default is strictly better -- override via the env var only if
# you ever need to point this at something else (e.g. a staging Space).
LIVE_VIEW_BASE_URL = os.environ.get("MESSA_LIVE_VIEW_BASE_URL", "https://live.textmessa.com").rstrip("/")

# ---- Sendblue (SMS/iMessage channel, Phase 2) ----
# Left optional (unlike OPENROUTER_API_KEY/DATABASE_URL above) so the CLI
# keeps working with no Sendblue account at all -- messa/server.py checks
# these itself and refuses to start if they're missing, since the webhook
# server has no reason to run without them.
SENDBLUE_API_KEY = os.environ.get("SENDBLUE_API_KEY")
SENDBLUE_API_SECRET = os.environ.get("SENDBLUE_API_SECRET")
SENDBLUE_NUMBER = os.environ.get("SENDBLUE_NUMBER") or "+14438062833"  # `sendblue lines` -- your assigned number
# Optional: only checked if you've set a secret on the webhook via
# `sendblue webhooks set-receive <url>` / the dashboard. Unset = no
# verification (fine for local dev behind a private tunnel; set it once
# the URL is public).
SENDBLUE_WEBHOOK_SECRET = os.environ.get("SENDBLUE_WEBHOOK_SECRET")
# Destructive deepsearch/email actions (clicks, typing, sending) normally
# block on a human "y/n" -- there's no synchronous stdin over a text
# conversation, so the server defaults to declining them (see
# approval.DenyApprovalGate). Set this true to auto-approve instead, once
# you're comfortable with that tradeoff for your own account.
SMS_AUTO_APPROVE_DESTRUCTIVE = os.environ.get("MESSA_SMS_AUTO_APPROVE_DESTRUCTIVE", "true").strip().lower() in (
    "1", "true", "yes",
)

# ---- Identity of the local CLI user ----
# Phase 1 runs single-user over the CLI (no phone/email channel yet), so we
# pin a fixed dev identity. Phase 2 will resolve this per-channel instead
# (from the inbound SMS/email sender) rather than a constant. Name is left
# unset by default (rather than a placeholder) so a brand-new dev user goes
# through Messa's onboarding flow instead of skipping it.
DEFAULT_CLI_PHONE = os.environ.get("MESSA_CLI_USER_PHONE", "+10000000000")
DEFAULT_CLI_NAME = os.environ.get("MESSA_CLI_USER_NAME") or None
DEFAULT_TIMEZONE = os.environ.get("MESSA_DEFAULT_TIMEZONE", "America/New_York")

# ---- Runtime tuning ----
RECURSION_LIMIT = int(os.environ.get("MESSA_RECURSION_LIMIT", "100"))
SESSIONS_DIR = os.environ.get("MESSA_SESSIONS_DIR", "sessions")
OUTPUTS_DIR = os.environ.get("MESSA_OUTPUTS_DIR", "outputs")


def resolve_output_file(path: str, *, max_bytes: int | None = None) -> Path:
    """Resolve and validate a path a tool/model reported as a file it
    generated -- exists, is a real file, and lives inside OUTPUTS_DIR (so a
    model-invented or path-traversal path can never make a caller read an
    arbitrary file off the server), optionally under a raw-byte size cap.
    Raises ValueError with a clear, specific reason on any failure rather
    than letting a confusing lower-level exception (FileNotFoundError, a
    PermissionError from escaping the output dir, etc.) surface instead.

    Factored out of channels/resend.py's original _validate_attachment_path
    (which now just calls this) so the same security-critical check backs
    every outbound-attachment path in the project, not just email's --
    tools/personal_inbox_tools.py's send_email/reply_to_email (via
    channels/resend.py) and agents/registry.py's send_pdf_over_text
    (outbound SMS/iMessage attachment) both go through this one
    implementation instead of duplicating the directory-traversal defense."""
    outputs_dir = Path(OUTPUTS_DIR).resolve()
    try:
        resolved = Path(path).resolve()
    except OSError as e:
        raise ValueError(f"Couldn't resolve path {path!r}: {e}") from e
    if not resolved.is_relative_to(outputs_dir):
        raise ValueError(
            f"Path {path!r} isn't inside the outputs directory -- only a file a tool actually "
            "produced can be used here."
        )
    if not resolved.is_file():
        raise ValueError(f"File not found: {path!r}.")
    if max_bytes is not None:
        size = resolved.stat().st_size
        if size > max_bytes:
            limit_mb = max_bytes / (1024 * 1024)
            actual_mb = size / (1024 * 1024)
            raise ValueError(f"File is too large ({actual_mb:.1f}MB, limit is {limit_mb:.0f}MB).")
    return resolved

# Deepsearch (the browsing/research subagent, formerly called
# "browser_agent"): domains it is allowed to navigate to. Empty = no restriction.
DEEPSEARCH_ALLOWED_DOMAINS: list[str] = [
    d.strip() for d in os.environ.get("MESSA_DEEPSEARCH_ALLOWED_DOMAINS", "").split(",") if d.strip()
]
# No more DEEPSEARCH_HEADLESS / DEEPSEARCH_PROFILES_ROOT -- now that the
# browser itself lives on Browserbase, not in our container, headless mode
# and profile-directory persistence are Browserbase's problem (the latter
# is now a per-user Browserbase Context instead, see db.py's
# get_browserbase_context_id). Both settings are dead now that deepsearch
# talks to a remote browser instead of launching a local one.
# Step budget for a single deepsearch run (one delegation). Separate from
# the orchestrator's own RECURSION_LIMIT -- deep research tasks legitimately
# need many steps, and hitting this cap is expected/handled (the session is
# saved and can be resumed) rather than an error condition.
# 80 steps gives ample headroom to navigate, handle modals, and submit forms without mid-task cutoff.
DEEPSEARCH_MAX_STEPS = int(os.environ.get("MESSA_DEEPSEARCH_MAX_STEPS", "80"))

# Automatically recover the last visited URL on resumed sessions if the new browser opens at about:blank
DEEPSEARCH_AUTO_NAVIGATE_ON_RESUME = bool(os.environ.get("MESSA_DEEPSEARCH_AUTO_NAVIGATE_ON_RESUME", "1").lower() in ("1", "true", "yes"))

# Deterministic backstop for snapshot-size discipline (the "reinforce/verify
# the snapshot-size guidance" option flagged, unimplemented, in the speed
# investigation -- now implemented as part of the token-cost work, since a
# large snapshot is exactly as expensive in tokens as it is slow in
# latency). DEEPSEARCH_SYSTEM_PROMPT already tells the model to prefer
# browser_find/browser_snapshot(depth=...) over a full snapshot, but nothing
# enforced it -- a model that ignores that guidance on a big page pays for
# the oversized snapshot again on EVERY subsequent step for the rest of the
# run (it's resent in full, every time, until compaction evicts it -- see
# SUBAGENT_EFFECTIVE_CONTEXT_TOKENS above). guarded()'s browser_snapshot
# success path appends a corrective nudge (same "one corrective nudge into
# the same graph thread" shape as the existing auth-wall backstop) when a
# non-depth-limited snapshot comes back over this many characters. Picked
# as a round, generous number with no real production traffic to calibrate
# against -- deliberately loose enough not to fire on an ordinary page, so
# tune it down if real usage shows it's too permissive.
DEEPSEARCH_LARGE_SNAPSHOT_CHARS = int(
    os.environ.get("MESSA_DEEPSEARCH_LARGE_SNAPSHOT_CHARS", "12000")
)

# Second tier of the same backstop, per the CTO discussion on speeding up
# each sub-agent's "reading the page" step: DEEPSEARCH_LARGE_SNAPSHOT_CHARS
# above only ever appends a corrective NUDGE -- it doesn't shrink the
# snapshot the model is about to spend time (and tokens) parsing THIS turn,
# only asks it to behave differently NEXT time. That leaves the single
# worst case -- a genuinely enormous page -- fully unaddressed for the call
# that actually triggered it. This is the deterministic cap on that: a
# snapshot beyond this many characters gets its content TRUNCATED (not just
# nudged), directly cutting how much the model has to read and how many
# tokens the call costs, right now. Deliberately set well above
# DEEPSEARCH_LARGE_SNAPSHOT_CHARS (not the same value) so an ordinary
# "large-ish" page -- the common case the nudge is meant to correct
# behavior on -- is never truncated, only genuinely extreme outliers where
# losing some content is a better trade than parsing all of it. Picked as a
# round number with no real production traffic to calibrate against, same
# caveat as DEEPSEARCH_LARGE_SNAPSHOT_CHARS -- tune down if real usage shows
# even this is too permissive, or up if legitimate pages are getting cut.
DEEPSEARCH_SNAPSHOT_HARD_CAP_CHARS = int(
    os.environ.get("MESSA_DEEPSEARCH_SNAPSHOT_HARD_CAP_CHARS", "60000")
)

# How long (ms) @playwright/mcp waits after each action for triggered work
# (a re-render, an XHR, an animation) to "settle" before returning control --
# an unconditional per-action wait, not a timeout ceiling like the two
# below. The package's own default (500ms) is a conservative one-size-fits-
# all value; most pages settle well under that, so this is turned down a
# bit as a real, if modest, per-action latency win across every single
# click/type/navigate deepsearch makes. Not verified against a live
# Browserbase session from this sandbox (no live account here) -- if you
# start seeing "stale snapshot"/race-condition-looking failures on slower
# sites after this change, raise it back toward 500.
DEEPSEARCH_TIMEOUT_SETTLE_MS = int(os.environ.get("MESSA_DEEPSEARCH_TIMEOUT_SETTLE_MS", "200"))

# Visible cursor on the live view (see tools/deepsearch_tools.py's
# BrowserToolProvider._move_cursor_to and messa/assets/cursor_overlay.js).
#
# Two things changed here after a real run showed (a) the arrow never
# actually appeared in the live-view tiles the user was watching, and (b)
# there IS a genuine speed cost worth naming: this evaluate() call used to
# be awaited inline, before the real click/type/hover/select_option it
# decorates -- meaning every one of those actions paid the cursor's own
# ~380ms CSS-transition round trip as pure added latency, serialized in
# front of the real action. Fixed by firing it as a background task instead
# (fire-and-forget, same established pattern as _start_reading_animation
# below) -- the real action no longer waits on the cosmetic arrow at all,
# so this flag is no longer a meaningful latency tradeoff, just a pure
# visual nicety. The separate real-human-cursor driver (a genuine extra
# Node subprocess talking CDP to Browserbase's remote browser --
# messa/nodehelpers/cursor_driver.mjs) was removed entirely (not just
# disabled) after the same run: it added a whole extra process/connection
# per run for a cursor that wasn't even visible, which is a bad trade
# regardless of the latency fix above. What's left is the original,
# already-proven-in-production DOM-only CSS-transition arrow -- turn this
# off (false) if you'd rather skip it altogether; the browser session opens/
# closes exactly the same either way, only the cosmetic cursor-move calls
# (now free of any latency cost) are skipped.
DEEPSEARCH_CURSOR_OVERLAY = os.environ.get("MESSA_DEEPSEARCH_CURSOR_OVERLAY", "true").strip().lower() in (
    "1", "true", "yes",
)

# Click ripple + typing highlight (messa/assets/cursor_overlay.js's
# window.__messaEffects) -- the alternative visual feedback asked for once
# the human-cursor turned out not to even be visible: an expanding ring at
# the click point, and a brief colored outline on whatever field is being
# typed into. Both ride the SAME evaluate() call the cursor-arrow move
# already fires for browser_click/browser_type (see
# BrowserToolProvider._move_cursor_to) -- no extra round trip beyond what
# the arrow alone already costs, and (per DEEPSEARCH_CURSOR_OVERLAY's own
# comment above) that call is fire-and-forget now, so neither of these adds
# any latency to the real action either. Independently toggleable from the
# arrow itself -- e.g. turn the arrow off but keep these, if the arrow ever
# turns out not to be worth it either.
DEEPSEARCH_CLICK_RIPPLE = os.environ.get("MESSA_DEEPSEARCH_CLICK_RIPPLE", "true").strip().lower() in (
    "1", "true", "yes",
)
DEEPSEARCH_TYPING_HIGHLIGHT = os.environ.get("MESSA_DEEPSEARCH_TYPING_HIGHLIGHT", "true").strip().lower() in (
    "1", "true", "yes",
)

# A brief full-page color flash on every fresh document load (see
# cursor_overlay.js's self-triggering IIFE -- it needs no call from Python
# at all, since the whole init-script re-runs on every navigation by
# construction) -- "page-transition animation" from the CTO discussion:
# something other than a frozen-looking page during a navigate's own
# network/load time, which none of the other cosmetic features (arrow,
# ripple, typing highlight, reading-scroll) cover, since none of them fire
# during a navigate itself.
DEEPSEARCH_PAGE_TRANSITION_FLASH = os.environ.get(
    "MESSA_DEEPSEARCH_PAGE_TRANSITION_FLASH", "true"
).strip().lower() in ("1", "true", "yes")

# "Reading" animation on the live view (per explicit user request: deepsearch
# sitting on a static page for several seconds of LLM think-time between a
# browser_snapshot and its next action reads as "frozen," not "working"). A
# background asyncio task (see tools/deepsearch_tools.py's
# BrowserToolProvider._start_reading_animation) fires right after each
# successful browser_snapshot and scroll-centers a few real content elements
# in sequence, in a discrete "hop, pause, hop, settle" pattern rather than
# one smooth scroll -- entirely cosmetic, running in parallel with (not
# blocking) the actual agent loop, and stopped again the instant the model's
# next real tool call arrives (see _stop_reading_animation, called at the
# top of every guarded() call). Confirmed empirically in this sandbox
# (/tmp/test_mcp_concurrency.py) that @playwright/mcp pipelines concurrent
# tool calls over its stdio transport rather than serializing them, so a
# long-running background browser_evaluate call does not block/delay the
# real action's own MCP round trip. Shares messa/assets/cursor_overlay.js's
# injection (both features ride the same --init-script) but is independently
# toggleable -- turning this off still leaves the click/type cursor overlay
# on, and vice versa.
DEEPSEARCH_READING_ANIMATION = os.environ.get("MESSA_DEEPSEARCH_READING_ANIMATION", "true").strip().lower() in (
    "1", "true", "yes",
)

# Human-in-the-loop pause: deepsearch hits a login wall/CAPTCHA/2FA, calls
# request_human_help, and waits -- see tools/deepsearch_tools.py's
# request_human_help tool, db.py's deepsearch_human_help_requests functions,
# and server.py's _production_deepsearch_pause_loop for the full flow.
# Timings, in order of how the wait actually plays out:
#   1. Wait DEEPSEARCH_HUMAN_HELP_INITIAL_WAIT_SECONDS for ANY activity at
#      all (60s, your number) -- nothing at all by then means give up now.
#   2. Once activity is seen, extend in
#      DEEPSEARCH_HUMAN_HELP_EXTEND_SECONDS (20s) increments for as long as
#      fresh activity keeps appearing each increment.
#   3. Regardless of how active it still looks,
#      DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS (300s = 5 minutes, your
#      number) is a hard ceiling on the total wait -- this is what actually
#      bounds the cost, not the "looks active" heuristic, since "looks
#      active" alone could in principle keep extending forever.
# DEEPSEARCH_HUMAN_HELP_POLL_INTERVAL_SECONDS (5s) is how often
# request_human_help itself checks the page for change while waiting (a
# different, tighter cadence than the poll LOOP in server.py, which only
# needs to catch a new row quickly enough to send one timely SMS).
DEEPSEARCH_HUMAN_HELP_INITIAL_WAIT_SECONDS = int(
    os.environ.get("MESSA_DEEPSEARCH_HUMAN_HELP_INITIAL_WAIT_SECONDS", "60")
)
DEEPSEARCH_HUMAN_HELP_EXTEND_SECONDS = int(
    os.environ.get("MESSA_DEEPSEARCH_HUMAN_HELP_EXTEND_SECONDS", "20")
)
DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS = int(
    os.environ.get("MESSA_DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS", "300")
)
DEEPSEARCH_HUMAN_HELP_POLL_INTERVAL_SECONDS = int(
    os.environ.get("MESSA_DEEPSEARCH_HUMAN_HELP_POLL_INTERVAL_SECONDS", "5")
)

# ---- Email verification-code relay (migrations/028_deepsearch_otp_
# expectations.sql, tools/deepsearch_tools.py's await_email_verification_
# code) ----
# A narrower, fully-automatic cousin of DEEPSEARCH_HUMAN_HELP_* above: when
# deepsearch signs up for a site using the user's own Messa-assigned inbox
# (generate_account_credential's default username), a verification code
# lands somewhere no HUMAN can read either -- request_human_help's "wait for
# a person to take over the live view" design can't help here at all, so
# this polls a durable DB row instead, resolved by server.py's inbound-
# email webhook the moment the real email arrives (usually a few seconds,
# not minutes) rather than by page-fingerprint change. Deliberately its own
# shorter timeout, not reusing DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS:
# an OTP email either shows up within a few tens of seconds or it isn't
# coming (bounced, filtered, wrong address) -- there's no reason to hold a
# billed Browserbase session open for 5 minutes on the same one-size
# ceiling a HUMAN needing to notice an SMS and go act was tuned for.
DEEPSEARCH_OTP_WAIT_MAX_SECONDS = int(
    os.environ.get("MESSA_DEEPSEARCH_OTP_WAIT_MAX_SECONDS", "90")
)
DEEPSEARCH_OTP_WAIT_POLL_INTERVAL_SECONDS = int(
    os.environ.get("MESSA_DEEPSEARCH_OTP_WAIT_POLL_INTERVAL_SECONDS", "2")
)

# Belt-and-suspenders hard ceiling on one deepsearch run's total wall-clock
# time (wraps inner_agent.ainvoke(...) in tools/deepsearch_tools.py's _run
# with asyncio.wait_for), independent of DEEPSEARCH_MAX_STEPS (a step COUNT,
# not a time bound -- a run stuck making slow real-world requests could
# stay under the step cap while still running for a very long time) and
# independent of the human-help timings above. This is the actual answer to
# "don't leave a rogue browser session eating up money": even a bug
# somewhere in the request_human_help wait/extend/give-up logic can't hold
# a Browserbase session open past this, because it's enforced one level
# above that feature, not only inside it. 1200s = 20 minutes -- comfortably
# above DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS (5 min) plus room for
# genuine multi-step research work around it.
DEEPSEARCH_MAX_SESSION_SECONDS = int(os.environ.get("MESSA_DEEPSEARCH_MAX_SESSION_SECONDS", "1200"))

# v1 launch switch: live web browsing/deepsearch is OFF for the general
# public at launch (every Browserbase session is real, metered money, and
# the feature needs more real-world runway before it's public-ready) --
# open only to hand-picked beta/investor accounts, via
# UserContext.deepsearch_beta_access (migrations/029_deepsearch_beta_
# access.sql, granted with a manual `UPDATE users SET
# deepsearch_beta_access = true WHERE phone_number = ...`, same convention
# as users.is_admin). THIS is the one flag to flip for public release:
# set MESSA_DEEPSEARCH_PUBLIC_ACCESS=true and every user gets access
# immediately -- no code change, no migration, no per-user column to
# populate. See UserContext.has_deepsearch_access for the actual
# combined check every call site uses.
DEEPSEARCH_PUBLIC_ACCESS = os.environ.get("MESSA_DEEPSEARCH_PUBLIC_ACCESS", "false").strip().lower() in ("1", "true", "yes")

# Scalability fix (pre-1000-user launch): a hard, process-wide cap on how
# many TOP-LEVEL deepsearch sessions can be running at once, across every
# user sharing this one container. Before this, there was no such
# ceiling -- DEEPSEARCH_MAX_SUBAGENTS just below only bounds sub-worker
# tabs WITHIN one already-running session. Each top-level session spawns a
# real `npx @playwright/mcp` Node child process plus a Browserbase
# session, so with no cap, a burst of concurrent deepsearch requests
# (plausible the moment you have hundreds of real users) could spawn one
# Node process per request, exhausting the container's CPU/RAM/file
# descriptors, or blow past whatever your Browserbase plan's own
# concurrent-session limit is (session creation would just start failing
# from there). Enforced by a single process-wide asyncio.Semaphore in
# tools/deepsearch_tools.py, acquired before anything expensive happens
# (before the Browserbase session, before the Node subprocess) and
# released once the run fully ends. Default of 20 leaves headroom under
# the real, confirmed ceiling for your current Browserbase plan (Developer,
# $20/mo: 25 concurrent browsers -- this must never be set >= that number,
# or session creation starts failing instead of queueing cleanly once
# you're actually at capacity). Raise this to ~80-90 if/when you upgrade to
# the Startup plan ($99/mo, 100 concurrent browsers) -- see README's
# "Scaling" section. Note this isn't the only Browserbase ceiling that
# matters at launch: the Developer plan also only includes 100 browser-
# HOURS/month ($0.12/hr overage beyond that) -- at DEEPSEARCH_MAX_SESSION_
# SECONDS's own 20-minute cap per session, that's roughly 300 sessions/
# month total across every user before overage kicks in, which a 100-user
# launch can plausibly reach well before hitting the concurrency cap.
# Watch Browserbase's own usage dashboard for browser-hours, not just
# concurrent-session count.
DEEPSEARCH_MAX_CONCURRENT_SESSIONS = int(
    os.environ.get("MESSA_DEEPSEARCH_MAX_CONCURRENT_SESSIONS", "20")
)
# How long a request waits for a free slot (queued behind the cap above)
# before giving up and telling the user we're at capacity, rather than
# leaving them (and their asyncio task) hanging indefinitely behind a long
# queue. Deliberately short relative to how long a user will patiently
# wait for a text back -- past this, "try again in a bit" is a better
# experience than silence.
DEEPSEARCH_SESSION_QUEUE_WAIT_SECONDS = int(
    os.environ.get("MESSA_DEEPSEARCH_SESSION_QUEUE_WAIT_SECONDS", "45")
)

# Multi-site delegation (delegate_website_task, tools/deepsearch_tools.py):
# how many sub-worker tabs can be running concurrently at once, across
# however many delegate_website_task calls the model makes in one turn.
# Enforced by an asyncio.Semaphore shared for the whole deepsearch run, not
# by trusting the model to count correctly -- calls beyond the cap simply
# wait for a slot. Deliberately starting LOW (your call: start at 3, raise
# later once this has run for real) rather than at the 5 you originally
# suggested -- each concurrent tab is another live browser connection (and,
# once real human-cursor lands, another Node-side cursor instance), so the
# cost of getting the cap wrong is directly a cost-and-reliability one, not
# just a performance tuning knob.
DEEPSEARCH_MAX_SUBAGENTS = int(os.environ.get("MESSA_DEEPSEARCH_MAX_SUBAGENTS", "3"))

# Step budget for ONE delegate_website_task sub-worker -- deliberately
# smaller than DEEPSEARCH_MAX_STEPS (the top-level deepsearch agent's own
# budget covering however many sites it delegates in total): a sub-worker is
# scoped to exactly ONE site and ONE goal, not the whole task, mirroring what
# deepsearch used to do sequentially per site before multi-site delegation
# existed -- not a new, more open-ended kind of task. Kept deliberately
# tight (your explicit ask: "I don't want these sessions to go longer") --
# enough for navigate -> snapshot -> a couple of actions -> a final read, not
# a multi-page exploration.
DEEPSEARCH_SUBAGENT_MAX_STEPS = int(os.environ.get("MESSA_DEEPSEARCH_SUBAGENT_MAX_STEPS", "8"))

# Second backstop under DEEPSEARCH_MAX_SESSION_SECONDS (1200s), added after a
# real production incident: a Browserbase session that died mid-run (user
# closed it manually; Browserbase's own timeout would do the same) kept
# getting retried by the top-level model for 11 more full LLM turns (~330s
# of pure OpenRouter spend) before the log cut off, because every tool
# failure was just handed back as "try a different approach" -- there IS no
# different approach once the session is gone, but the model had no way to
# know that. tools/deepsearch_tools.py's guarded() now raises immediately
# (ending the run) once EITHER of two things is true: (a) the failure is
# specifically a confirmed-dead-session error (410/"Gone"/"session not
# running" -- see _is_dead_browser_session_error, handled unconditionally,
# not gated by this constant at all), or (b) this many tool calls in a row
# have failed for ANY reason on one tab -- a broader net for a run that's
# clearly stuck even without that exact signature. Deliberately generous
# (6, not 2-3): a few real retries in a row is normal exploration (a
# selector that needs adjusting, a slow page), not yet evidence of being
# stuck -- this is a backstop for a genuinely runaway loop, not a
# first-resort brake. Per-tab (each BrowserToolProvider/sub-worker tracks
# its own count), so one stuck sub-worker doesn't end an otherwise-healthy
# top-level run by itself -- see _delegate_website_task's own comment on
# why only the dead-session case (a) propagates past a sub-worker's own
# error handling to end the whole run, while this one (b) doesn't.
DEEPSEARCH_MAX_CONSECUTIVE_TOOL_ERRORS = int(
    os.environ.get("MESSA_DEEPSEARCH_MAX_CONSECUTIVE_TOOL_ERRORS", "6")
)

# Step budget for executive_assistant (tasks/reminders/notes/contacts/
# calendar), deliberately small and separate from DEEPSEARCH_MAX_STEPS --
# see tools/executive_tools.py's build_executive_subagent docstring. This
# role is a handful of direct DB calls plus at most one deterministic
# quality-check retry, not an open-ended research loop, so a large step
# budget would only let a confused run wander instead of failing fast.
EXECUTIVE_RECURSION_LIMIT = int(os.environ.get("MESSA_EXECUTIVE_RECURSION_LIMIT", "14"))

# Step budget for email_agent (the user's own Gmail via Composio) -- same
# reasoning as EXECUTIVE_RECURSION_LIMIT just above, and deliberately given
# its own constant rather than reusing that one: reading/searching/sending/
# replying to Gmail, or walking someone through the connect-link flow, is a
# handful of direct Composio calls, not an open-ended research loop --
# giving it deepsearch's ~100-step ambient budget (what a plain declarative
# SubAgent dict inherits by default, since nothing in this codebase capped
# it before) would only let a confused run wander through retries instead
# of failing fast and telling Messa what actually went wrong. See
# tools/email_tools.py's build_email_subagent.
EMAIL_RECURSION_LIMIT = int(os.environ.get("MESSA_EMAIL_RECURSION_LIMIT", "12"))

# All server-side "now" comparisons (due reminders/cron) use tz-aware UTC
# datetimes explicitly (datetime.now(timezone.utc)) rather than naive
# datetime.now(), so this holds regardless of what timezone the host
# machine/container is actually set to (Neon + your deployment target both
# run in UTC).
SERVER_TIMEZONE_NOTE = "UTC"


# ---- Default morning/evening briefings ----
# Auto-provisioned for every user -- new (at account creation) and existing
# (backfilled the next time they're loaded, see db.ensure_default_briefings
# and cli.load_user_context) -- rather than something the user has to think
# to ask Messa for. Each entry becomes one cron_jobs row tagged with this
# key in the new `kind` column (migrations/011_cron_job_kind.sql), which is
# what lets ensure_default_briefings tell "already has one" apart from
# "never had one" without re-creating a briefing a user deliberately
# cancelled (existence of a row with this kind, regardless of its status,
# means "leave it alone").
#
# The prompt text below is exactly what fires through the same
# load_user_context -> build_orchestrator -> run_message path a real
# inbound text would use (see server.py's _production_cron_loop) -- there's
# no separate "briefing renderer," Messa just answers this prompt for real
# every time, using whatever's actually in the DB (and a live web_search for
# weather) at that moment.
DEFAULT_BRIEFINGS: dict[str, dict[str, str]] = {
    "morning_briefing": {
        "cron_expression": "0 7 * * *",  # 7:00 AM, the user's own local time
        "prompt_or_task": (
            "Give me my morning briefing as a short text. Check today's calendar events "
            "and any tasks due today or overdue, and list them briefly. Include a short, "
            "one-line weather summary for today for my location (use search_web for "
            "current weather -- don't guess). If I have nothing scheduled and no tasks "
            "due, don't just say so -- remind me you're happy to help set something up, "
            "or take care of anything on my mind, including things that need actually "
            "clicking around a website, since you can browse the web for me. Keep it "
            "warm, brief, and easy to scan."
        ),
    },
    "evening_briefing": {
        "cron_expression": "0 20 * * *",  # 8:00 PM, the user's own local time
        "prompt_or_task": (
            "Give me my evening briefing as a short text. Summarize what I got done "
            "today -- tasks completed and events that happened -- then give me a quick "
            "look at tomorrow: tomorrow's schedule and anything due tomorrow or still "
            "overdue. If today was quiet (nothing completed, nothing scheduled), say so "
            "briefly and remind me you're around to help plan tomorrow or take care of "
            "anything on my mind, including browsing the web for me if it'd help. Keep "
            "it warm, brief, and easy to scan."
        ),
    },
}

# For existing users who already had a simple, hand-set-up morning/evening
# briefing BEFORE this feature existed (a plain user-created cron job, via
# propose_create_routine -- `kind` is NULL on those rows, since
# tagging didn't exist yet). Per an explicit "replace entirely" product
# decision: rather than leave that old job running alongside a brand new
# tagged one (a real user would get double-texted every morning),
# db.ensure_default_briefings looks for exactly one untagged job whose own
# prompt_or_task text contains one of these keywords and retires
# (cancels) it before creating the new one. Deliberately conservative --
# see that function's docstring for why zero or multiple matches means
# "don't touch anything, just create the new one normally" rather than
# guessing.
LEGACY_BRIEFING_MATCH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "morning_briefing": ("morning brief",),
    "evening_briefing": ("evening brief",),
}

# ---- Weather (messa/weather.py) ----
# Open-Meteo's forecast API -- same provider timeutil.py's geocoding
# already uses (free, keyless, 10,000 calls/day on the non-commercial free
# tier -- comfortably enough for two briefings/day/user at any realistic
# user count), so this project has exactly one third-party weather
# dependency to reason about instead of two. Explicit product decision: no
# fallback provider -- if this call fails or returns something implausible,
# messa/weather.py returns None and the caller (briefings.py) drops the
# weather line entirely rather than show a wrong or stale number.
WEATHER_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_TIMEOUT_SECONDS = float(os.environ.get("MESSA_WEATHER_TIMEOUT_SECONDS", "8"))
WEATHER_TEMPERATURE_UNIT = os.environ.get("MESSA_WEATHER_TEMPERATURE_UNIT", "fahrenheit")  # or "celsius"
# How many days out to request in one call -- 2 covers both briefings from a
# single fetch (today for the morning briefing, tomorrow for the evening
# briefing's "quick look at tomorrow").
WEATHER_FORECAST_DAYS = int(os.environ.get("MESSA_WEATHER_FORECAST_DAYS", "2"))

# ---- Task routines (migrations/024_task_routines.sql) ----
# Bounds for the "Messa does it herself" (execution_mode='autonomous') side
# of routines_agent -- kept as env-editable knobs (same HFS-editable-config
# convention as the usage-limits plans in messa/plans.py) rather than
# hardcoded, since these are exactly the kind of number you'd want to tune
# without a code change: how long a background watcher is allowed to keep
# retrying before it gives up and tells the user, how many failed attempts
# it gets, how quickly it backs off, and how insistent it's allowed to get
# when the user isn't responding to something time-sensitive.
#
# Every one of these is a DEFAULT applied when a routine doesn't specify its
# own value (propose_create_routine lets the model set a tighter or looser
# bound per-routine) -- never a hard ceiling the model can't ask to extend,
# except ROUTINE_MAX_EXPIRE_HOURS, which IS a hard ceiling: no autonomous
# routine, however it's configured, can run longer than that without either
# finishing itself or getting rescheduled by the user, so a background loop
# can never become an unbounded, silently-running cost sink.
ROUTINE_DEFAULT_EXPIRE_HOURS = float(os.environ.get("MESSA_ROUTINE_DEFAULT_EXPIRE_HOURS", "48"))
ROUTINE_MAX_EXPIRE_HOURS = float(os.environ.get("MESSA_ROUTINE_MAX_EXPIRE_HOURS", "168"))  # 7 days
ROUTINE_MAX_RETRY_ATTEMPTS = int(os.environ.get("MESSA_ROUTINE_MAX_RETRY_ATTEMPTS", "5"))
ROUTINE_RETRY_BACKOFF_MINUTES = int(os.environ.get("MESSA_ROUTINE_RETRY_BACKOFF_MINUTES", "30"))
# "While you were away" digest (#3): local hour a user's queued digest
# items get flushed into one message, plus a backstop count so someone with
# a lot of background activity isn't left waiting a full day for the first
# result.
ROUTINE_DIGEST_HOUR_LOCAL = int(os.environ.get("MESSA_ROUTINE_DIGEST_HOUR_LOCAL", "8"))
ROUTINE_DIGEST_BACKSTOP_COUNT = int(os.environ.get("MESSA_ROUTINE_DIGEST_BACKSTOP_COUNT", "3"))
# Polite escalation on non-response (#9): bounded so it can never become
# spam -- each unanswered check-in shrinks the wait before the next one
# (BASE_MINUTES * SHRINK_FACTOR^attempts, floored at MIN_MINUTES), and once
# ROUTINE_ESCALATION_MAX_COUNT unanswered check-ins have gone out, the
# routine sends one last message saying so and stops on its own rather than
# continuing to ping at any pace, however gentle, forever.
ROUTINE_ESCALATION_MAX_COUNT = int(os.environ.get("MESSA_ROUTINE_ESCALATION_MAX_COUNT", "3"))
ROUTINE_ESCALATION_BASE_MINUTES = int(os.environ.get("MESSA_ROUTINE_ESCALATION_BASE_MINUTES", "60"))
ROUTINE_ESCALATION_SHRINK_FACTOR = float(os.environ.get("MESSA_ROUTINE_ESCALATION_SHRINK_FACTOR", "0.5"))
ROUTINE_ESCALATION_MIN_MINUTES = int(os.environ.get("MESSA_ROUTINE_ESCALATION_MIN_MINUTES", "15"))

# ---- New-user cap + waitlist (migrations/025_new_user_cap_waitlist.sql,
# messa/waitlist.py) ----
# How many NEW users (signed up after this cap was first turned on -- see
# db.get_new_user_baseline_id for how "new" is defined, auto-captured so
# existing users are never retroactively counted) are allowed in before
# server.py's _process_inbound starts waitlisting instead of onboarding.
# 0 (the default) means unlimited -- completely unset, this changes nothing
# about the existing app; only set this to actually turn the cap on.
NEW_USER_CAP = int(os.environ.get("MESSA_NEW_USER_CAP", "0"))

# ---- Admin broadcast (migrations/026_admin_broadcast.sql,
# messa/tools/admin_tools.py) ----
# Caps how many Sendblue sends a single broadcast fires at once (an
# asyncio.Semaphore in server.py's _run_one_broadcast) -- same reasoning as
# DEEPSEARCH_MAX_SUBAGENTS' own semaphore: broadcasting to a large user base
# shouldn't fire thousands of concurrent HTTP requests at Sendblue's API at
# once.
BROADCAST_MAX_CONCURRENT_SENDS = int(os.environ.get("MESSA_BROADCAST_MAX_CONCURRENT_SENDS", "20"))

# ---- Production poll loop concurrency (server.py) ----
# _production_cron_loop and _production_digest_loop used to process their
# whole due batch one job/user at a time in a plain `for` loop -- fine at a
# handful of users, but at launch-scale traffic (1000+ users, a shared
# 5-minute-ish poll cadence) a batch can contain many due jobs at once, and
# an "autonomous" cron job is a FULL re-invoked Messa LLM turn -- one slow
# model call or one user's stuck job would otherwise hold up everyone else's
# reminder behind it in the same poll tick. Bounded concurrency here mirrors
# _run_one_broadcast's existing asyncio.Semaphore + asyncio.gather shape
# (BROADCAST_MAX_CONCURRENT_SENDS above) rather than introducing a new
# pattern. Kept well below DEEPSEARCH_MAX_CONCURRENT_SESSIONS's concern
# (these are LLM calls, not new Browserbase sessions/Node subprocesses) but
# still bounded, since unbounded fan-out of LLM turns is its own way to
# saturate the box (CPU, memory, and OpenRouter rate limits all at once).
CRON_MAX_CONCURRENT_JOBS = int(os.environ.get("MESSA_CRON_MAX_CONCURRENT_JOBS", "10"))
DIGEST_MAX_CONCURRENT_SENDS = int(os.environ.get("MESSA_DIGEST_MAX_CONCURRENT_SENDS", "20"))


@dataclass
class UserContext:
    """Resolved identity for whoever Messa is currently talking to.

    Phase 1: always the CLI user. Phase 2: built from the inbound
    channel (phone number for SMS/iMessage, address for email) via
    db.get_or_create_user().
    """

    user_id: int
    phone_number: str
    name: str | None = None
    email: str | None = None
    city: str | None = None
    # From users.city_prompt_skipped/email_prompt_skipped
    # (migrations/031_profile_prompt_skips.sql): set when the user explicitly
    # said 'skip' to a profile-enrichment ask (db.save_profile_field) --
    # agents/registry.py's profile-enrichment prompt block checks these so
    # it never keeps re-suggesting a field someone already declined. False
    # (never skipped) on an un-migrated DB, same as everywhere else.
    city_prompt_skipped: bool = False
    email_prompt_skipped: bool = False
    timezone: str = DEFAULT_TIMEZONE
    # Whether `timezone` was actually derived from something the user told
    # us (their city/zip, via timeutil.resolve_timezone) as opposed to
    # still being the hardcoded DEFAULT_TIMEZONE placeholder. False means
    # "don't fully trust this for scheduling yet" -- see
    # timeutil.current_context_str and executive_tools.py's system prompt.
    timezone_confirmed: bool = False
    onboarding_step: str = "complete"
    channel: str = "cli"
    extra: dict = field(default_factory=dict)
    live_view_token: str | None = None
    # Cached from users.email_connected (migrations/012_email_connection.sql)
    # -- cheap per-turn read so the system prompt/onboarding logic knows
    # without an extra Composio API call. Never the source of truth for
    # whether a Gmail action will actually succeed (Composio's own response
    # is); see email_tools.py.
    email_connected: bool = False
    # From users.messa_email_local_part (migrations/013_personal_email.sql),
    # provisioned automatically the first time a user's context loads (see
    # cli.load_user_context / db.get_or_create_messa_email_local_part) --
    # None only if that migration hasn't been applied yet. Not the same
    # thing as `email` above (that's the user's own personal address on
    # file, collected during onboarding); this is the address Messa herself
    # owns on TEXTMESSA_EMAIL_DOMAIN. See messa_email below for the full
    # address string.
    messa_email_local_part: str | None = None
    # From users.default_email_provider (migrations/015_default_email_provider.sql):
    # which inbox Messa treats as the default for a generic "send/check my
    # email" request that doesn't name one -- "messa" (their own address
    # above) or "gmail" (their connected Gmail, email_tools.py). Defaults
    # to "messa" here too (not just at the DB column's own DEFAULT), so
    # behavior is identical whether or not migration 015 has run yet. Only
    # ever changed by the user's own explicit request -- see
    # agents/registry.py's set_default_email_provider tool; connecting
    # Gmail does NOT change this by itself.
    default_email_provider: str = "messa"
    # From users.plan_id (migrations/023_usage_limits.sql) -- a free-text
    # key into plans.ACTIVE_PLANS, not a Postgres ENUM (see that
    # migration's own comment for why). Defaults to plans.DEFAULT_PLAN_ID
    # here too, matching every other column above that mirrors its own
    # DB-side default, so behavior is identical whether or not migration
    # 023 has run yet. plans.get_plan() never raises on an unrecognized
    # value -- it falls back to the default plan instead.
    plan_id: str = plans.DEFAULT_PLAN_ID
    # From users.is_admin (migrations/023_usage_limits.sql) -- a quiet,
    # unadvertised bypass for the dev team's own accounts, checked in
    # exactly one place (usage.check_and_consume) rather than scattered
    # through every hook point. Never surfaced on the pricing page or in
    # plans.py; set with a manual `UPDATE users SET is_admin = true`.
    is_admin: bool = False
    # From users.deepsearch_beta_access (migrations/029_deepsearch_beta_
    # access.sql) -- same quiet, manually-flipped-by-SQL shape as is_admin
    # just above, deliberately kept as its own separate column rather than
    # folded into is_admin or plan_id: v1 launches with live web
    # browsing/deepsearch turned OFF for the public (Browserbase sessions
    # cost real money per-session and the whole feature needs more runway
    # before it's public-ready), open only to a handful of hand-picked
    # beta/investor accounts. See DEEPSEARCH_PUBLIC_ACCESS below and
    # has_deepsearch_access's own docstring for the actual gate this
    # column feeds -- going public later never touches this column at all,
    # it's a single config flip.
    deepsearch_beta_access: bool = False
    # From users.memory_profile (migrations/027_memory.sql) -- the cheap,
    # always-on profile digest, read once per turn via db.get_memory_profile
    # exactly like the fields above, NOT a live Mem0 call (see
    # config.MEM0_ENABLED/messa/memory.py for the actual memory backend --
    # this field is just the last digest that backend already wrote).
    # None for a brand-new user or before their first daily batch run --
    # _build_system_prompt omits it entirely in that case, same "absent
    # field, not a placeholder" pattern as city/email above.
    memory_profile: str | None = None
    # For inbound iMessage turns via Sendblue, the handle of the incoming message bubble.
    # Allows Messa to send tapback reactions directly to this message.
    message_handle: str | None = None

    @property
    def onboarding_complete(self) -> bool:
        return self.onboarding_step == "complete"

    @property
    def has_deepsearch_access(self) -> bool:
        """The single gate tools/deepsearch_tools.py's build_deepsearch_
        subagent._run checks, first thing, before even the usage-limit
        check -- an ungated user's request never touches the DB usage
        counter, never opens a Browserbase session, never spends a cent.
        True for: DEEPSEARCH_PUBLIC_ACCESS (the v1 -> public-release
        switch -- flip this one env var and every user everywhere gets
        access immediately, no code change, no migration, no per-user
        flag to set), or an admin account (is_admin), or a hand-picked
        beta account (deepsearch_beta_access, granted with a manual
        `UPDATE users SET deepsearch_beta_access = true WHERE phone_number
        = ...`, same convention as is_admin). Deliberately a property here
        (not a bare module-level check in deepsearch_tools.py) so every
        caller -- the subagent gate, the "deepsearch isn't available yet"
        prompt hint in _build_system_prompt -- reads the exact same
        decision, and there's only one place this logic could ever drift."""
        return DEEPSEARCH_PUBLIC_ACCESS or self.is_admin or self.deepsearch_beta_access

    @property
    def messa_email(self) -> str | None:
        """The user's full Messa-owned address (e.g. "jane@textmessa.com"),
        or None if it hasn't been provisioned yet (migration 013 not
        applied). See tools/personal_inbox_tools.py."""
        if not self.messa_email_local_part:
            return None
        return f"{self.messa_email_local_part}@{TEXTMESSA_EMAIL_DOMAIN}"

    @property
    def messa_display_name(self) -> str:
        """The From display name every outbound Messa-email send/reply uses
        (channels/resend.py's `from_name`), so a recipient's mail client
        shows "Messa, personal assistant of Jane" rather than a bare
        address -- the user's own explicit ask. Falls back to a name-less
        phrasing for the rare case Messa doesn't have a name for this user
        yet (a send that somehow fires before onboarding collects one)."""
        if self.name:
            return f"Messa, personal assistant of {self.name}"
        return "Messa, your personal assistant"

    @property
    def live_view_share_url(self) -> str | None:
        """The link Messa texts alongside every deepsearch delegation, or
        None if either migration 006 hasn't run yet (no token) or
        LIVE_VIEW_BASE_URL isn't configured yet -- either way, the system
        prompt treats None as "don't mention a live link this turn"."""
        if not self.live_view_token or not LIVE_VIEW_BASE_URL:
            return None
        return f"{LIVE_VIEW_BASE_URL}/live/{self.live_view_token}"
