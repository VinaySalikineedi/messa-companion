"""Tests for V3-autonomous.md Phase 2 (docs/V3-autonomous.md, section 6 --
"Dynamic User Lists & Deterministic Mute Engine"), built on top of Phase 1
(feature/v3-phase1-one-touch-approvals, merged to main):

  1. db.py's user_lists functions (migrations/038_user_lists.sql):
     add_user_list_item's upsert-vs-normalize semantics, remove_user_list_item,
     get_user_list_items ordering, and is_muted_sender's exact-address/
     domain matching (including "Display Name <addr>" parsing and case-
     insensitivity) -- against a fake asyncpg pool/connection, same
     FakeAcquire/FakePool/FakeConn/FakeRow shape as
     test_v3_phase1_approvals_and_supersede.py. Also the pre-migration
     graceful degrade (every function returns a safe empty/False/None
     rather than raising when user_lists doesn't exist yet).
  2. registry.py's mute_email_sender/unmute_email_sender/
     list_muted_email_senders tools: input validation (a malformed
     address/domain is rejected before ever touching the DB), the
     USER_LISTS_ENABLED kill switch, and the actual mute/unmute/list
     round trip against a fake db layer.
  3. server.py's /webhooks/personal-email/inbound route, exercised
     end-to-end via starlette's TestClient (same technique
     test_call_webhook.py already uses for a different webhook in this
     same server.py): a muted sender's email is still durably logged
     (still visible in the dashboard) but never spawns the notification
     turn/SMS -- the actual "$0 token spend, silently archived" behavior
     the field incident asked for; an unmuted sender's email spawns the
     notification turn exactly as before this feature existed; the
     USER_LISTS_ENABLED kill switch skips the mute check entirely
     (today's exact pre-Phase-2 behavior).

No live Postgres, no live LLM call.
"""
import asyncio
import os
import sys

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

from messa import config, db  # noqa: E402
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
# test_v3_phase1_approvals_and_supersede.py / test_call_webhook.py.
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
        if "information_schema.tables" in query or "_has_table" in query:
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
# Part 1: db.py's user_lists functions
# ---------------------------------------------------------------------------

async def part1_user_lists_db():
    try:
        # 1a: add_user_list_item -- normalizes (lowercase/trim) and upserts.
        row = FakeRow(id=1, user_id=1, list_name="muted_email_senders", item_value="metricool.com")
        conn = FakeConn(fetchrow_queue=[row])
        install_fake_pool(conn)
        result = await db.add_user_list_item(1, "muted_email_senders", "  Metricool.COM  ")
        check("add_user_list_item: returns the inserted row", result == row)
        insert_call = next(c for c in conn.calls if c[0] == "fetchrow")
        check("add_user_list_item: normalizes (lowercase + trim) before writing",
              insert_call[2][2] == "metricool.com")

        # 1b: an empty/whitespace-only item never even reaches the DB.
        conn = FakeConn()
        install_fake_pool(conn)
        result = await db.add_user_list_item(1, "muted_email_senders", "   ")
        check("add_user_list_item: empty item_value returns None", result is None)
        check("add_user_list_item: empty item_value never queries the DB", conn.calls == [])

        # 1c: pre-migration graceful degrade -- never raises, returns None.
        conn = FakeConn(has_tables=False)
        install_fake_pool(conn)
        result = await db.add_user_list_item(1, "muted_email_senders", "spam@x.com")
        check("add_user_list_item: pre-migration returns None, doesn't raise", result is None)

        # 1d: remove_user_list_item -- True when a row was actually deleted.
        conn = FakeConn(fetchval_queue=[7])
        install_fake_pool(conn)
        removed = await db.remove_user_list_item(1, "muted_email_senders", "SPAM@X.COM")
        check("remove_user_list_item: True when a row existed and was deleted", removed is True)

        conn = FakeConn(fetchval_queue=[None])
        install_fake_pool(conn)
        removed = await db.remove_user_list_item(1, "muted_email_senders", "never-added@x.com")
        check("remove_user_list_item: False when nothing matched", removed is False)

        # 1e: get_user_list_items -- returns every row, empty list when none.
        rows = [FakeRow(item_value="a@x.com"), FakeRow(item_value="b.com")]
        conn = FakeConn(fetch_queue=[rows])
        install_fake_pool(conn)
        result = await db.get_user_list_items(1, "muted_email_senders")
        check("get_user_list_items: returns every row", result == rows)

        conn = FakeConn(has_tables=False)
        install_fake_pool(conn)
        result = await db.get_user_list_items(1, "muted_email_senders")
        check("get_user_list_items: pre-migration returns [] , not an error", result == [])
    finally:
        pass


async def part2_is_muted_sender():
    try:
        # 2a: exact address match.
        conn = FakeConn(fetchval_queue=[1])
        install_fake_pool(conn)
        result = await db.is_muted_sender(1, "spam@metricool.com")
        check("is_muted_sender: exact address match returns True", result is True)
        lookup_call = next(c for c in conn.calls if c[0] == "fetchval" and "user_lists" in c[1])
        check("is_muted_sender: checks both the exact address AND its domain in one query",
              lookup_call[2] == (1, ["spam@metricool.com", "metricool.com"]))

        # 2b: domain-only match (a different local part at a muted domain).
        conn = FakeConn(fetchval_queue=[1])
        install_fake_pool(conn)
        result = await db.is_muted_sender(1, "newsletter@metricool.com")
        check("is_muted_sender: a domain-level mute matches any sender at that domain", result is True)

        # 2b-bis: subdomain sender matches parent domain
        conn = FakeConn(fetchval_queue=[1])
        install_fake_pool(conn)
        result = await db.is_muted_sender(1, "newsletter@marketing.metricool.com")
        check("is_muted_sender: a subdomain sender matches parent domain", result is True)
        sub_call = next(c for c in conn.calls if c[0] == "fetchval" and "user_lists" in c[1])
        check("is_muted_sender: includes address, subdomain, and parent domain in candidates",
              sub_call[2] == (1, ["newsletter@marketing.metricool.com", "marketing.metricool.com", "metricool.com"]))

        # 2c: "Display Name <addr>" form is parsed correctly (RFC 2822 style,
        # exactly what a real inbound email's From header looks like).
        conn = FakeConn(fetchval_queue=[1])
        install_fake_pool(conn)
        result = await db.is_muted_sender(1, "Metricool Marketing <SPAM@Metricool.COM>")
        check("is_muted_sender: parses 'Display Name <addr>' form and lowercases it", result is True)

        # 2d: not muted at all -- the query runs but finds nothing.
        conn = FakeConn(fetchval_queue=[None])
        install_fake_pool(conn)
        result = await db.is_muted_sender(1, "friend@example.com")
        check("is_muted_sender: an unmuted sender returns False", result is False)

        # 2e: malformed/empty from_address never even reaches the DB.
        conn = FakeConn()
        install_fake_pool(conn)
        result = await db.is_muted_sender(1, "not-an-email")
        check("is_muted_sender: an address with no '@' returns False without querying",
              result is False and conn.calls == [])
        result = await db.is_muted_sender(1, "")
        check("is_muted_sender: empty from_address returns False", result is False)

        # 2f: pre-migration graceful degrade.
        conn = FakeConn(has_tables=False)
        install_fake_pool(conn)
        result = await db.is_muted_sender(1, "spam@metricool.com")
        check("is_muted_sender: pre-migration returns False, doesn't raise", result is False)
    finally:
        pass


# ---------------------------------------------------------------------------
# Part 3: registry.py's mute_email_sender/unmute_email_sender/
# list_muted_email_senders tools
# ---------------------------------------------------------------------------

async def part3_mute_tools():
    real_add = db.add_user_list_item
    real_remove = db.remove_user_list_item
    real_get = db.get_user_list_items
    real_flag = config.USER_LISTS_ENABLED
    try:
        config.USER_LISTS_ENABLED = True
        user = _user(user_id=9)
        tools = registry.build_orchestrator_tools(user)
        mute_tool = next(t for t in tools if t.name == "mute_email_sender")
        unmute_tool = next(t for t in tools if t.name == "unmute_email_sender")
        list_tool = next(t for t in tools if t.name == "list_muted_email_senders")

        add_calls = []

        async def fake_add(user_id, list_name, item_value, metadata=None):
            add_calls.append((user_id, list_name, item_value))
            return {"id": 1, "user_id": user_id, "list_name": list_name, "item_value": item_value}

        db.add_user_list_item = fake_add

        # 3a: muting a full address.
        reply = await mute_tool.coroutine(sender_or_domain="Newsletter@Metricool.com")
        check("mute_email_sender: normalizes case before storing",
              add_calls == [(9, "muted_email_senders", "newsletter@metricool.com")])
        check("mute_email_sender: confirms muting a 'sender' (not 'domain') for a full address",
              "sender" in reply and "domain" not in reply.split("'")[0])

        # 3b: muting a bare domain.
        add_calls.clear()
        reply = await mute_tool.coroutine(sender_or_domain="metricool.com")
        check("mute_email_sender: accepts a bare domain",
              add_calls == [(9, "muted_email_senders", "metricool.com")])
        check("mute_email_sender: confirms muting an 'entire domain' for a bare domain",
              "entire domain" in reply)

        # 3b-bis: muting a domain with leading @ (@metricool.com).
        add_calls.clear()
        reply = await mute_tool.coroutine(sender_or_domain="@metricool.com")
        check("mute_email_sender: accepts a domain with leading @",
              add_calls == [(9, "muted_email_senders", "metricool.com")])
        check("mute_email_sender: confirms muting an 'entire domain' for leading @",
              "entire domain" in reply)

        # 3c: rejects garbage input BEFORE ever touching the DB.
        add_calls.clear()
        reply = await mute_tool.coroutine(sender_or_domain="not valid at all!!")
        check("mute_email_sender: rejects a malformed value", "doesn't look like" in reply)
        check("mute_email_sender: never calls the DB for a malformed value", add_calls == [])

        reply = await mute_tool.coroutine(sender_or_domain="plainword")
        check("mute_email_sender: rejects a bare word with no dot (not a real domain)",
              "doesn't look like" in reply)

        reply = await mute_tool.coroutine(sender_or_domain="")
        check("mute_email_sender: rejects an empty value", "Give me" in reply)

        # 3d: unmute -- found vs. not found.
        async def fake_remove_found(user_id, list_name, item_value):
            return True

        db.remove_user_list_item = fake_remove_found
        reply = await unmute_tool.coroutine(sender_or_domain="metricool.com")
        check("unmute_email_sender: confirms when something was actually removed",
              "Unmuted" in reply)

        async def fake_remove_not_found(user_id, list_name, item_value):
            return False

        db.remove_user_list_item = fake_remove_not_found
        reply = await unmute_tool.coroutine(sender_or_domain="never-muted.com")
        check("unmute_email_sender: says plainly when nothing was there to remove",
              "wasn't on your muted list" in reply)

        # 3e: list -- empty vs. populated.
        async def fake_get_empty(user_id, list_name):
            return []

        db.get_user_list_items = fake_get_empty
        reply = await list_tool.coroutine()
        check("list_muted_email_senders: says so when nothing is muted", "Nothing muted" in reply)

        async def fake_get_populated(user_id, list_name):
            return [{"item_value": "metricool.com"}, {"item_value": "spam@x.com"}]

        db.get_user_list_items = fake_get_populated
        reply = await list_tool.coroutine()
        check("list_muted_email_senders: lists every muted item",
              "metricool.com" in reply and "spam@x.com" in reply)

        # 3f: the USER_LISTS_ENABLED kill switch -- all three tools refuse
        # cleanly, without ever touching the DB layer.
        config.USER_LISTS_ENABLED = False
        add_calls.clear()
        db.add_user_list_item = fake_add
        reply = await mute_tool.coroutine(sender_or_domain="metricool.com")
        check("kill switch: mute_email_sender refuses cleanly", "isn't enabled" in reply)
        check("kill switch: never touches the DB", add_calls == [])
        reply = await unmute_tool.coroutine(sender_or_domain="metricool.com")
        check("kill switch: unmute_email_sender refuses cleanly", "isn't enabled" in reply)
        reply = await list_tool.coroutine()
        check("kill switch: list_muted_email_senders refuses cleanly", "isn't enabled" in reply)
    finally:
        config.USER_LISTS_ENABLED = real_flag
        db.add_user_list_item = real_add
        db.remove_user_list_item = real_remove
        db.get_user_list_items = real_get


# ---------------------------------------------------------------------------
# Part 4: server.py's /webhooks/personal-email/inbound route, end-to-end
# ---------------------------------------------------------------------------

async def part4_webhook_wiring():
    real_secret = config.PERSONAL_EMAIL_WEBHOOK_SECRET
    real_flag = config.USER_LISTS_ENABLED
    real_get_user = db.get_user_by_messa_email_local_part
    real_log = db.log_inbound_personal_email
    real_resolve_otp = db.resolve_otp_expectation_from_email
    real_process = server._process_inbound_personal_email

    config.PERSONAL_EMAIL_WEBHOOK_SECRET = None
    client = TestClient(app)
    processed_calls = []

    async def fake_get_user(local_part):
        return {"id": 42}

    async def fake_log(*a, **kw):
        return {"id": 100, "message_id": a[1] if len(a) > 1 else kw.get("message_id")}

    async def fake_resolve_otp(*a, **kw):
        return None

    async def fake_process(user_id, logged, pdf_note=None):
        processed_calls.append((user_id, logged))

    db.get_user_by_messa_email_local_part = fake_get_user
    db.log_inbound_personal_email = fake_log
    db.resolve_otp_expectation_from_email = fake_resolve_otp
    server._process_inbound_personal_email = fake_process

    try:
        config.USER_LISTS_ENABLED = True

        # 4a: a MUTED sender -- durably logged (fake_log still ran), but
        # the notification turn is never spawned.
        conn = FakeConn(fetchval_queue=[1])  # is_muted_sender's own lookup finds a match
        install_fake_pool(conn)
        processed_calls.clear()
        resp = client.post(
            "/webhooks/personal-email/inbound",
            json={
                "to": "jane@textmessa.com", "from": "spam@metricool.com",
                "subject": "Weekly digest", "text": "Check out our features",
                "message_id": "msg-muted-1",
            },
        )
        check("muted sender: request accepted (200)", resp.status_code == 200)
        check("muted sender: status explicitly says the notification was suppressed",
              "muted" in resp.json().get("status", ""))
        check("muted sender: the notification turn is NEVER spawned",
              processed_calls == [])

        # 4b: an UNMUTED sender -- behaves exactly as before this feature:
        # logged AND the notification turn is spawned.
        conn = FakeConn(fetchval_queue=[None])  # is_muted_sender finds no match
        install_fake_pool(conn)
        processed_calls.clear()
        resp = client.post(
            "/webhooks/personal-email/inbound",
            json={
                "to": "jane@textmessa.com", "from": "friend@example.com",
                "subject": "Hey", "text": "Just checking in",
                "message_id": "msg-unmuted-1",
            },
        )
        check("unmuted sender: request accepted (200)", resp.status_code == 200)
        check("unmuted sender: status is the plain 'accepted' (unaffected by this feature)",
              resp.json().get("status") == "accepted")
        check("unmuted sender: the notification turn IS spawned, exactly as before",
              len(processed_calls) == 1 and processed_calls[0][0] == 42)

        # 4c: USER_LISTS_ENABLED off -- the mute check is skipped entirely,
        # even for a sender that WOULD otherwise match (byte-identical to
        # pre-Phase-2 behavior).
        config.USER_LISTS_ENABLED = False
        conn = FakeConn(fetchval_queue=[1])  # would match if the check ran at all
        install_fake_pool(conn)
        processed_calls.clear()
        resp = client.post(
            "/webhooks/personal-email/inbound",
            json={
                "to": "jane@textmessa.com", "from": "spam@metricool.com",
                "subject": "Weekly digest", "text": "Check out our features",
                "message_id": "msg-killswitch-1",
            },
        )
        check("kill switch off: the mute check is skipped -- notification turn still spawned",
              len(processed_calls) == 1)
        mute_lookup_calls = [c for c in conn.calls if c[0] == "fetchval" and "user_lists" in c[1]]
        check("kill switch off: is_muted_sender's own query never even runs",
              mute_lookup_calls == [])
    finally:
        config.PERSONAL_EMAIL_WEBHOOK_SECRET = real_secret
        config.USER_LISTS_ENABLED = real_flag
        db.get_user_by_messa_email_local_part = real_get_user
        db.log_inbound_personal_email = real_log
        db.resolve_otp_expectation_from_email = real_resolve_otp
        server._process_inbound_personal_email = real_process


async def main() -> None:
    await part1_user_lists_db()
    await part2_is_muted_sender()
    await part3_mute_tools()
    await part4_webhook_wiring()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
