"""Tests for V3-autonomous.md Phase 3 ("Three-Tier Inbound Email Triage"),
built on top of Phase 2 (migrations/038_user_lists.sql, merged to main):

  1. messa/email_triage.py's pure classifier: classify_inbound_email's
     VIP/daily/weekly heuristic (receipt/update signal beats marketing
     signal beats ambiguous-automated-localpart beats the VIP default),
     summarize_for_digest's one-line rendering, and
     tiers_for_briefing_kind's morning-always/evening-only-on-Sunday rule.
  2. db.is_vip_sender: the user-controlled override that forces a sender
     back to the VIP tier regardless of what the heuristic would say,
     against a fake asyncpg pool/connection (same FakeAcquire/FakePool/
     FakeConn/FakeRow shape as test_v3_phase2_user_lists.py).
  3. db.py's email_digest_queue functions (migrations/039_email_digest_
     queue.sql): enqueue/get_pending/clear round trip, and the
     pre-migration graceful degrade.
  4. messa/email_triage.render_digest_section against a faked db layer.
  5. registry.py's mark_email_vip/unmark_email_vip/list_vip_email_senders
     tools: input validation, the EMAIL_TRIAGE_ENABLED kill switch, and the
     actual mark/unmark/list round trip -- same shape as Phase 2's mute
     tool tests.
  6. server.py's /webhooks/personal-email/inbound route, exercised
     end-to-end via starlette's TestClient: a VIP-looking sender still
     spawns the normal notification turn (today's exact pre-Phase-3
     behavior); a receipt/update-looking sender is queued into the
     'daily' tier and the notification turn is NEVER spawned; a
     marketing-looking sender is queued into the 'weekly' tier, same;
     the EMAIL_TRIAGE_ENABLED kill switch skips triage entirely (today's
     exact pre-Phase-3 behavior, byte-identical).

No live Postgres, no live LLM call.
"""
import asyncio
import os
import sys

from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")
    os.environ.setdefault("SENDBLUE_API_KEY", "sb-test-dummy-key")
    os.environ.setdefault("SENDBLUE_API_SECRET", "sb-test-dummy-secret")

import types as _types
_fake_composio_exceptions = _types.ModuleType("composio.exceptions")


class ComposioMultipleConnectedAccountsError(Exception):
    pass


_fake_composio_exceptions.ComposioMultipleConnectedAccountsError = ComposioMultipleConnectedAccountsError
_fake_composio_pkg = _types.ModuleType("composio")
_fake_composio_pkg.exceptions = _fake_composio_exceptions
sys.modules["composio"] = _fake_composio_pkg
sys.modules["composio.exceptions"] = _fake_composio_exceptions

from starlette.testclient import TestClient  # noqa: E402

from messa import config, db, email_triage  # noqa: E402
from messa.agents import registry  # noqa: E402
import messa.server as server  # noqa: E402
from messa.server import app  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _user(**kwargs) -> config.UserContext:
    base = dict(user_id=1, phone_number="+15550000000", name="Jane", onboarding_step="complete")
    base.update(kwargs)
    return config.UserContext(**base)


# ---------------------------------------------------------------------------
# Fakes -- same FakeAcquire/FakePool/FakeConn/FakeRow shape as
# test_v3_phase2_user_lists.py.
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, has_tables=True, fetchrow_queue=None, fetch_queue=None, fetchval_queue=None):
        self.has_tables = has_tables
        self.fetchrow_queue = list(fetchrow_queue or [])
        self.fetch_queue = list(fetch_queue or [])
        self.fetchval_queue = list(fetchval_queue or [])
        self.calls = []

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "information_schema.tables" in query:
            return self.has_tables
        if self.fetchval_queue:
            return self.fetchval_queue.pop(0)
        return self.has_tables

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if self.fetchrow_queue:
            return self.fetchrow_queue.pop(0)
        return None

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if self.fetch_queue:
            return self.fetch_queue.pop(0)
        return []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "UPDATE 0"


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class FakeRow(dict):
    """asyncpg.Record is dict-like; dict(row) is used throughout db.py."""


def install_fake_pool(conn):
    async def fake_get_pool():
        return FakePool(conn)
    db.get_pool = fake_get_pool


# ---------------------------------------------------------------------------
# Part 1: classify_inbound_email's pure heuristic (USER_LISTS_ENABLED off,
# so no DB call/pool is needed at all for these).
# ---------------------------------------------------------------------------

async def part1_classify_heuristic():
    real_flag = config.USER_LISTS_ENABLED
    try:
        config.USER_LISTS_ENABLED = False

        tier = await email_triage.classify_inbound_email(
            1, "no-reply@amazon.com", "Your order has shipped", "Package on its way!",
        )
        check("classify: a shipping-confirmation subject -> 'daily'", tier == email_triage.TIER_DAILY)

        tier = await email_triage.classify_inbound_email(
            1, "newsletter@shop.com", "50% off - Unsubscribe here", "Huge sale this weekend",
        )
        check("classify: an unsubscribe/percent-off subject -> 'weekly'", tier == email_triage.TIER_WEEKLY)

        tier = await email_triage.classify_inbound_email(
            1, "notifications@github.com", "New comment on your pull request", "Someone commented",
        )
        check(
            "classify: automated-looking sender with no receipt/marketing signal -> 'daily' "
            "(ambiguous-but-automated resolves to the sooner tier, not 'weekly')",
            tier == email_triage.TIER_DAILY,
        )

        tier = await email_triage.classify_inbound_email(
            1, "friend@example.com", "lunch?", "hey, are we still on for lunch today?",
        )
        check("classify: an ordinary human email -> 'vip' (today's exact behavior)", tier == email_triage.TIER_VIP)

        # Receipt signal wins even when a marketing-shaped phrase also
        # appears in the body -- the whole point is a real invoice never
        # gets buried in a once-a-week digest just because its footer also
        # has an unsubscribe link.
        tier = await email_triage.classify_inbound_email(
            1, "billing@service.com", "Your invoice #4471",
            "Please find your invoice attached. Unsubscribe from billing emails in account settings.",
        )
        check(
            "classify: a receipt/invoice subject wins over a marketing signal in the body -> 'daily'",
            tier == email_triage.TIER_DAILY,
        )

        # A local part shaped like a real person, even if the body happens
        # to mention a delivery -- local-part-not-automated plus no
        # marketing/receipt SUBJECT keyword should not misfire to weekly.
        tier = await email_triage.classify_inbound_email(
            1, "dad@familymail.com", "Package for you",
            "Hey, a package was delivered to my place by mistake, come grab it whenever.",
        )
        check(
            "classify: a real-looking sender with a casual, non-automated subject -> 'vip'",
            tier == email_triage.TIER_VIP,
        )
    finally:
        config.USER_LISTS_ENABLED = real_flag


# ---------------------------------------------------------------------------
# Part 2: db.is_vip_sender + classify_inbound_email's override check
# ---------------------------------------------------------------------------

async def part2_vip_override():
    real_flag = config.USER_LISTS_ENABLED
    try:
        config.USER_LISTS_ENABLED = True

        # 2a: exact address match.
        conn = FakeConn(fetchval_queue=[1])
        install_fake_pool(conn)
        result = await db.is_vip_sender(1, "receipts@bank.com")
        check("is_vip_sender: exact address match returns True", result is True)
        lookup_call = next(c for c in conn.calls if c[0] == "fetchval" and "user_lists" in c[1])
        check("is_vip_sender: checks vip_email_senders (not muted_email_senders)",
              "vip_email_senders" in lookup_call[1])

        # 2b: domain match.
        conn = FakeConn(fetchval_queue=[1])
        install_fake_pool(conn)
        result = await db.is_vip_sender(1, "anyone@bank.com")
        check("is_vip_sender: a domain-level VIP mark matches any sender at that domain", result is True)

        # 2c: not marked -- False.
        conn = FakeConn(fetchval_queue=[None])
        install_fake_pool(conn)
        result = await db.is_vip_sender(1, "friend@example.com")
        check("is_vip_sender: an unmarked sender returns False", result is False)

        # 2d: pre-migration graceful degrade.
        conn = FakeConn(has_tables=False)
        install_fake_pool(conn)
        result = await db.is_vip_sender(1, "receipts@bank.com")
        check("is_vip_sender: pre-migration returns False, doesn't raise", result is False)

        # 2e: the override actually flips classify_inbound_email's result
        # for a sender that would otherwise clearly classify as 'daily'.
        conn = FakeConn(fetchval_queue=[1])  # is_vip_sender's own lookup finds a match
        install_fake_pool(conn)
        tier = await email_triage.classify_inbound_email(
            1, "receipts@bank.com", "Your monthly statement is ready", "Statement attached",
        )
        check(
            "classify_inbound_email: a VIP-marked sender is always 'vip', even with a "
            "receipt-shaped subject",
            tier == email_triage.TIER_VIP,
        )

        # 2f: USER_LISTS_ENABLED off -- the override check is skipped
        # entirely, never touching the DB, and the heuristic decides alone.
        config.USER_LISTS_ENABLED = False
        conn = FakeConn(fetchval_queue=[1])  # would match if the check ran at all
        install_fake_pool(conn)
        tier = await email_triage.classify_inbound_email(
            1, "receipts@bank.com", "Your monthly statement is ready", "Statement attached",
        )
        check("classify_inbound_email: USER_LISTS_ENABLED off skips the VIP-override lookup entirely",
              tier == email_triage.TIER_DAILY and conn.calls == [])
    finally:
        config.USER_LISTS_ENABLED = real_flag


# ---------------------------------------------------------------------------
# Part 3: summarize_for_digest + tiers_for_briefing_kind
# ---------------------------------------------------------------------------

async def part3_summarize_and_tiers():
    summary = email_triage.summarize_for_digest("no-reply@amazon.com", "Your order has shipped")
    check("summarize_for_digest: shows the domain and subject", summary == "amazon.com -- Your order has shipped")

    summary = email_triage.summarize_for_digest("no-reply@amazon.com", "")
    check("summarize_for_digest: falls back to '(no subject)' for an empty subject",
          summary == "amazon.com -- (no subject)")

    tiers = email_triage.tiers_for_briefing_kind("morning_briefing", datetime(2023, 1, 2))  # a Monday
    check("tiers_for_briefing_kind: morning_briefing always carries the daily tier",
          tiers == [email_triage.TIER_DAILY])

    tiers = email_triage.tiers_for_briefing_kind("evening_briefing", datetime(2023, 1, 1))  # a Sunday
    check("tiers_for_briefing_kind: evening_briefing on a local Sunday carries the weekly tier",
          tiers == [email_triage.TIER_WEEKLY])

    tiers = email_triage.tiers_for_briefing_kind("evening_briefing", datetime(2023, 1, 2))  # a Monday
    check("tiers_for_briefing_kind: evening_briefing on a non-Sunday carries nothing",
          tiers == [])

    tiers = email_triage.tiers_for_briefing_kind("some_other_kind", datetime(2023, 1, 1))
    check("tiers_for_briefing_kind: an unrecognized kind carries nothing", tiers == [])


# ---------------------------------------------------------------------------
# Part 4: db.py's email_digest_queue functions
# ---------------------------------------------------------------------------

async def part4_email_digest_db():
    # 4a: enqueue -- writes an INSERT with the given tier/summary.
    conn = FakeConn()
    install_fake_pool(conn)
    await db.enqueue_email_digest_item(7, "daily", "amazon.com -- Your order has shipped")
    insert_call = next(c for c in conn.calls if c[0] == "execute")
    check("enqueue_email_digest_item: inserts with the right args",
          insert_call[2] == (7, "daily", "amazon.com -- Your order has shipped"))

    # 4b: get_pending -- returns every row for that user+tier.
    rows = [FakeRow(summary="amazon.com -- shipped"), FakeRow(summary="shop.com -- delivered")]
    conn = FakeConn(fetch_queue=[rows])
    install_fake_pool(conn)
    result = await db.get_pending_email_digest_items(7, "daily")
    check("get_pending_email_digest_items: returns every queued row", result == rows)

    # 4c: clear -- deletes for that user+tier only.
    conn = FakeConn()
    install_fake_pool(conn)
    await db.clear_email_digest_items(7, "weekly")
    delete_call = next(c for c in conn.calls if c[0] == "execute")
    check("clear_email_digest_items: deletes with the right user_id/tier",
          delete_call[2] == (7, "weekly"))

    # 4d: pre-migration graceful degrade -- never raises.
    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    await db.enqueue_email_digest_item(7, "daily", "x")
    result = await db.get_pending_email_digest_items(7, "daily")
    await db.clear_email_digest_items(7, "daily")
    check("email_digest_queue functions: pre-migration is a safe no-op, doesn't raise",
          result == [])


# ---------------------------------------------------------------------------
# Part 5: render_digest_section + clear_flushed_digest_tiers
# ---------------------------------------------------------------------------

async def part5_render_and_clear():
    real_get_pending = db.get_pending_email_digest_items
    real_clear = db.clear_email_digest_items
    real_flag = config.EMAIL_TRIAGE_ENABLED
    try:
        config.EMAIL_TRIAGE_ENABLED = True

        # 5a: no tiers due -> None, no DB call.
        calls = []

        async def fake_get_pending(user_id, tier):
            calls.append((user_id, tier))
            return []

        db.get_pending_email_digest_items = fake_get_pending
        result = await email_triage.render_digest_section(7, [])
        check("render_digest_section: empty tiers list -> None, no DB call", result is None and calls == [])

        # 5b: one queued item -> singular phrasing.
        async def fake_get_pending_one(user_id, tier):
            return [FakeRow(summary="amazon.com -- Your order has shipped")]

        db.get_pending_email_digest_items = fake_get_pending_one
        result = await email_triage.render_digest_section(7, [email_triage.TIER_DAILY])
        check("render_digest_section: one item renders as 'Emails: ...'",
              result == "Emails: amazon.com -- Your order has shipped.")

        # 5c: multiple queued items -> counted, semicolon-joined.
        async def fake_get_pending_many(user_id, tier):
            return [FakeRow(summary="a.com -- one"), FakeRow(summary="b.com -- two")]

        db.get_pending_email_digest_items = fake_get_pending_many
        result = await email_triage.render_digest_section(7, [email_triage.TIER_DAILY])
        check("render_digest_section: multiple items are counted and joined",
              result == "Emails (2): a.com -- one; b.com -- two.")

        # 5d: weekly tier gets the marketing-specific label.
        result = await email_triage.render_digest_section(7, [email_triage.TIER_WEEKLY])
        check("render_digest_section: weekly tier uses the marketing/promo label",
              result.startswith("This week's marketing/promo emails"))

        # 5e: nothing queued -> None (never blanks/crashes the briefing).
        async def fake_get_pending_empty(user_id, tier):
            return []

        db.get_pending_email_digest_items = fake_get_pending_empty
        result = await email_triage.render_digest_section(7, [email_triage.TIER_DAILY])
        check("render_digest_section: nothing queued -> None", result is None)

        # 5f: EMAIL_TRIAGE_ENABLED off -> None even with tiers given, no DB call.
        config.EMAIL_TRIAGE_ENABLED = False
        calls.clear()
        db.get_pending_email_digest_items = fake_get_pending
        result = await email_triage.render_digest_section(7, [email_triage.TIER_DAILY])
        check("render_digest_section: kill switch off -> None, never touches the DB",
              result is None and calls == [])
        config.EMAIL_TRIAGE_ENABLED = True

        # 5g: clear_flushed_digest_tiers clears exactly the tier(s)
        # tiers_for_briefing_kind says apply right now -- morning_briefing
        # always clears 'daily', regardless of what day it happens to be.
        clear_calls = []

        async def fake_clear(user_id, tier):
            clear_calls.append((user_id, tier))

        db.clear_email_digest_items = fake_clear
        job = {"user_id": 7, "kind": "morning_briefing", "user_timezone": "America/New_York"}
        await email_triage.clear_flushed_digest_tiers(job)
        check("clear_flushed_digest_tiers: morning_briefing clears the daily tier",
              clear_calls == [(7, email_triage.TIER_DAILY)])
    finally:
        db.get_pending_email_digest_items = real_get_pending
        db.clear_email_digest_items = real_clear
        config.EMAIL_TRIAGE_ENABLED = real_flag


# ---------------------------------------------------------------------------
# Part 6: registry.py's mark_email_vip/unmark_email_vip/list_vip_email_senders
# ---------------------------------------------------------------------------

async def part6_vip_registry_tools():
    real_add = db.add_user_list_item
    real_remove = db.remove_user_list_item
    real_get = db.get_user_list_items
    real_flag = config.EMAIL_TRIAGE_ENABLED
    try:
        config.EMAIL_TRIAGE_ENABLED = True
        user = _user(user_id=9)
        tools = registry.build_orchestrator_tools(user)
        mark_tool = next(t for t in tools if t.name == "mark_email_vip")
        unmark_tool = next(t for t in tools if t.name == "unmark_email_vip")
        list_tool = next(t for t in tools if t.name == "list_vip_email_senders")

        add_calls = []

        async def fake_add(user_id, list_name, item_value, metadata=None):
            add_calls.append((user_id, list_name, item_value))
            return {"id": 1, "user_id": user_id, "list_name": list_name, "item_value": item_value}

        db.add_user_list_item = fake_add

        # 6a: marking a full address.
        reply = await mark_tool.coroutine(sender_or_domain="Receipts@Bank.com")
        check("mark_email_vip: normalizes case before storing",
              add_calls == [(9, "vip_email_senders", "receipts@bank.com")])
        check("mark_email_vip: confirms marking a 'sender' for a full address",
              "sender" in reply)

        # 6b: marking a bare domain, including leading @.
        add_calls.clear()
        reply = await mark_tool.coroutine(sender_or_domain="@bank.com")
        check("mark_email_vip: accepts a domain with leading @",
              add_calls == [(9, "vip_email_senders", "bank.com")])
        check("mark_email_vip: confirms marking an 'entire domain'", "entire domain" in reply)

        # 6c: rejects garbage input before ever touching the DB.
        add_calls.clear()
        reply = await mark_tool.coroutine(sender_or_domain="not valid at all!!")
        check("mark_email_vip: rejects a malformed value", "doesn't look like" in reply)
        check("mark_email_vip: never calls the DB for a malformed value", add_calls == [])
        reply = await mark_tool.coroutine(sender_or_domain="")
        check("mark_email_vip: rejects an empty value", "Give me" in reply)

        # 6d: unmark -- found vs. not found.
        async def fake_remove_found(user_id, list_name, item_value):
            return True

        db.remove_user_list_item = fake_remove_found
        reply = await unmark_tool.coroutine(sender_or_domain="bank.com")
        check("unmark_email_vip: confirms when something was actually removed", "Unmarked" in reply)

        async def fake_remove_not_found(user_id, list_name, item_value):
            return False

        db.remove_user_list_item = fake_remove_not_found
        reply = await unmark_tool.coroutine(sender_or_domain="never-marked.com")
        check("unmark_email_vip: says plainly when nothing was there to remove",
              "wasn't marked VIP" in reply)

        # 6e: list -- empty vs. populated.
        async def fake_get_empty(user_id, list_name):
            return []

        db.get_user_list_items = fake_get_empty
        reply = await list_tool.coroutine()
        check("list_vip_email_senders: says so when nothing is marked", "Nothing marked VIP" in reply)

        async def fake_get_populated(user_id, list_name):
            return [{"item_value": "bank.com"}, {"item_value": "receipts@x.com"}]

        db.get_user_list_items = fake_get_populated
        reply = await list_tool.coroutine()
        check("list_vip_email_senders: lists every marked item",
              "bank.com" in reply and "receipts@x.com" in reply)

        # 6f: the EMAIL_TRIAGE_ENABLED kill switch -- all three tools
        # refuse cleanly, without ever touching the DB layer.
        config.EMAIL_TRIAGE_ENABLED = False
        add_calls.clear()
        db.add_user_list_item = fake_add
        reply = await mark_tool.coroutine(sender_or_domain="bank.com")
        check("kill switch: mark_email_vip refuses cleanly", "isn't enabled" in reply)
        check("kill switch: never touches the DB", add_calls == [])
        reply = await unmark_tool.coroutine(sender_or_domain="bank.com")
        check("kill switch: unmark_email_vip refuses cleanly", "isn't enabled" in reply)
        reply = await list_tool.coroutine()
        check("kill switch: list_vip_email_senders refuses cleanly", "isn't enabled" in reply)
    finally:
        config.EMAIL_TRIAGE_ENABLED = real_flag
        db.add_user_list_item = real_add
        db.remove_user_list_item = real_remove
        db.get_user_list_items = real_get


# ---------------------------------------------------------------------------
# Part 7: server.py's /webhooks/personal-email/inbound route, end-to-end
# ---------------------------------------------------------------------------

async def part7_webhook_wiring():
    real_secret = config.PERSONAL_EMAIL_WEBHOOK_SECRET
    real_triage_flag = config.EMAIL_TRIAGE_ENABLED
    real_lists_flag = config.USER_LISTS_ENABLED
    real_get_user = db.get_user_by_messa_email_local_part
    real_log = db.log_inbound_personal_email
    real_resolve_otp = db.resolve_otp_expectation_from_email
    real_process = server._process_inbound_personal_email
    real_enqueue = db.enqueue_email_digest_item

    config.PERSONAL_EMAIL_WEBHOOK_SECRET = None
    client = TestClient(app)
    processed_calls = []
    enqueue_calls = []

    async def fake_get_user(local_part):
        return {"id": 42}

    async def fake_log(*a, **kw):
        return {"id": 100, "message_id": a[1] if len(a) > 1 else kw.get("message_id")}

    async def fake_resolve_otp(*a, **kw):
        return None

    async def fake_process(user_id, logged, pdf_note=None):
        processed_calls.append((user_id, logged))

    async def fake_enqueue(user_id, tier, summary):
        enqueue_calls.append((user_id, tier, summary))

    db.get_user_by_messa_email_local_part = fake_get_user
    db.log_inbound_personal_email = fake_log
    db.resolve_otp_expectation_from_email = fake_resolve_otp
    server._process_inbound_personal_email = fake_process
    db.enqueue_email_digest_item = fake_enqueue

    try:
        config.EMAIL_TRIAGE_ENABLED = True
        config.USER_LISTS_ENABLED = True

        # 7a: a VIP-looking (ordinary human) sender -- behaves exactly as
        # before this feature: the notification turn is spawned, nothing
        # queued.
        conn = FakeConn(fetchval_queue=[None, None])  # not muted, not VIP-overridden
        install_fake_pool(conn)
        processed_calls.clear()
        enqueue_calls.clear()
        resp = client.post(
            "/webhooks/personal-email/inbound",
            json={
                "to": "jane@textmessa.com", "from": "friend@example.com",
                "subject": "lunch?", "text": "hey, are we still on for lunch today?",
                "message_id": "msg-vip-1",
            },
        )
        check("VIP sender: request accepted (200)", resp.status_code == 200)
        check("VIP sender: status is the plain 'accepted' (unaffected by this feature)",
              resp.json().get("status") == "accepted")
        check("VIP sender: the notification turn IS spawned, exactly as before",
              len(processed_calls) == 1 and processed_calls[0][0] == 42)
        check("VIP sender: nothing gets queued", enqueue_calls == [])

        # 7b: a receipt/update-looking sender -- queued into 'daily',
        # notification turn NEVER spawned.
        conn = FakeConn(fetchval_queue=[None, None])  # not muted, not VIP-overridden
        install_fake_pool(conn)
        processed_calls.clear()
        enqueue_calls.clear()
        resp = client.post(
            "/webhooks/personal-email/inbound",
            json={
                "to": "jane@textmessa.com", "from": "no-reply@amazon.com",
                "subject": "Your order has shipped", "text": "Package on its way!",
                "message_id": "msg-daily-1",
            },
        )
        check("daily-tier sender: request accepted (200)", resp.status_code == 200)
        check("daily-tier sender: status names the 'daily' tier",
              "daily" in resp.json().get("status", ""))
        check("daily-tier sender: the notification turn is NEVER spawned", processed_calls == [])
        check("daily-tier sender: queued with the right user/tier",
              len(enqueue_calls) == 1 and enqueue_calls[0][0] == 42 and enqueue_calls[0][1] == "daily")

        # 7c: a marketing-looking sender -- queued into 'weekly', same.
        conn = FakeConn(fetchval_queue=[None, None])
        install_fake_pool(conn)
        processed_calls.clear()
        enqueue_calls.clear()
        resp = client.post(
            "/webhooks/personal-email/inbound",
            json={
                "to": "jane@textmessa.com", "from": "newsletter@shop.com",
                "subject": "50% off - Unsubscribe here", "text": "Huge sale this weekend",
                "message_id": "msg-weekly-1",
            },
        )
        check("weekly-tier sender: status names the 'weekly' tier",
              "weekly" in resp.json().get("status", ""))
        check("weekly-tier sender: the notification turn is NEVER spawned", processed_calls == [])
        check("weekly-tier sender: queued with the right tier", enqueue_calls[0][1] == "weekly")

        # 7d: the EMAIL_TRIAGE_ENABLED kill switch -- triage is skipped
        # entirely, even for a sender that WOULD otherwise classify as
        # 'weekly' -- byte-identical to pre-Phase-3 behavior.
        config.EMAIL_TRIAGE_ENABLED = False
        conn = FakeConn(fetchval_queue=[None])  # only the mute check runs
        install_fake_pool(conn)
        processed_calls.clear()
        enqueue_calls.clear()
        resp = client.post(
            "/webhooks/personal-email/inbound",
            json={
                "to": "jane@textmessa.com", "from": "newsletter@shop.com",
                "subject": "50% off - Unsubscribe here", "text": "Huge sale this weekend",
                "message_id": "msg-killswitch-1",
            },
        )
        check("kill switch off: the notification turn still spawns", len(processed_calls) == 1)
        check("kill switch off: nothing gets queued", enqueue_calls == [])
        vip_lookup_calls = [c for c in conn.calls if c[0] == "fetchval" and "vip_email_senders" in c[1]]
        check("kill switch off: the VIP-override lookup never even runs", vip_lookup_calls == [])
    finally:
        config.PERSONAL_EMAIL_WEBHOOK_SECRET = real_secret
        config.EMAIL_TRIAGE_ENABLED = real_triage_flag
        config.USER_LISTS_ENABLED = real_lists_flag
        db.get_user_by_messa_email_local_part = real_get_user
        db.log_inbound_personal_email = real_log
        db.resolve_otp_expectation_from_email = real_resolve_otp
        server._process_inbound_personal_email = real_process
        db.enqueue_email_digest_item = real_enqueue


async def main() -> None:
    await part1_classify_heuristic()
    await part2_vip_override()
    await part3_summarize_and_tiers()
    await part4_email_digest_db()
    await part5_render_and_clear()
    await part6_vip_registry_tools()
    await part7_webhook_wiring()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
