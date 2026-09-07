"""Tests for Feature 1 of the "onboarding apps / reactions / message-split /
minimal-questions" round (see the four numbered asks in the session that
added migrations/032_app_connect_queue.sql): once onboarding completes,
Messa asks once what apps the user uses day to day, and if they answer with
several names, sends the connect links one at a time -- never a wall of
OAuth links at once.

Four parts:
  1. db.py: mark_apps_onboarding_asked / set_pending_app_connect_queue /
     pop_next_pending_app_connect against a hand-built fake asyncpg pool,
     including the pre-migration graceful-degrade path (same _has_column
     pattern as every other optional column in this project).
  2. cli.py: _onboarding_complete_messages appends the new apps-question
     reveal message exactly once (gated on apps_onboarding_asked), and
     marks it asked; a user who's already been asked never gets it again.
  3. tools/integration_tools.py: queue_app_connections dedupes/normalizes
     the incoming list, sends the FIRST app's link immediately (reusing
     send_connect_link), and queues the rest via
     db.set_pending_app_connect_queue -- without ever touching the
     existing, already-tested connect_integration_app tool.
  4. server.py: _production_app_connection_poll_loop, after marking a
     connection ACTIVE, pops the next queued app (if any) and sends ITS
     link too -- a user with nothing queued is completely unaffected.
"""
import asyncio
import os
import sys
import types

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")
    os.environ.setdefault("COMPOSIO_API_KEY", "comp-test-dummy-not-real")

# Fake `composio` package so integration_tools/server import cleanly without
# the real SDK installed -- same technique as test_disconnect_switch.py.
class ComposioMultipleConnectedAccountsError(Exception):
    pass


_fake_composio_exceptions = types.ModuleType("composio.exceptions")
_fake_composio_exceptions.ComposioMultipleConnectedAccountsError = ComposioMultipleConnectedAccountsError
_fake_composio_pkg = types.ModuleType("composio")
_fake_composio_pkg.exceptions = _fake_composio_exceptions
sys.modules["composio"] = _fake_composio_pkg
sys.modules["composio.exceptions"] = _fake_composio_exceptions

from messa import cli, config, db  # noqa: E402
from messa.channels import sendblue  # noqa: E402
from messa.tools import integration_tools as it  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# --- Part 1: db.py fake pool ---

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


def install_fake_pool(conn):
    async def fake_get_pool():
        return FakePool(conn)
    db.get_pool = fake_get_pool


class UsersConn:
    """Models just the two users columns migration 032 adds."""

    def __init__(self, row, has_column=True):
        self.row = row
        self.has_column = has_column

    async def fetchval(self, sql, *args):
        if "information_schema.columns" in sql:
            return self.has_column
        raise AssertionError(f"unexpected fetchval: {sql!r}")

    async def execute(self, sql, *args):
        stripped = sql.strip()
        if stripped == "UPDATE users SET apps_onboarding_asked = TRUE WHERE id = $1":
            (user_id,) = args
            self.row["apps_onboarding_asked"] = True
            return
        if stripped == "UPDATE users SET pending_app_connect_queue = $2 WHERE id = $1":
            _, queue_json = args
            self.row["pending_app_connect_queue"] = queue_json
            return
        raise AssertionError(f"unexpected execute: {sql!r}")

    async def fetchrow(self, sql, *args):
        stripped = sql.strip()
        if stripped == "SELECT pending_app_connect_queue FROM users WHERE id = $1":
            return {"pending_app_connect_queue": self.row.get("pending_app_connect_queue")}
        raise AssertionError(f"unexpected fetchrow: {sql!r}")


async def part1_db_functions():
    conn = UsersConn({"id": 1})
    install_fake_pool(conn)

    check("apps_onboarding_asked starts unset", conn.row.get("apps_onboarding_asked") is None)
    await db.mark_apps_onboarding_asked(1)
    check("mark_apps_onboarding_asked flips the flag TRUE", conn.row["apps_onboarding_asked"] is True)

    await db.set_pending_app_connect_queue(1, ["slack", "notion"])
    check("set_pending_app_connect_queue writes a JSON array (TEXT, not JSONB)",
          conn.row["pending_app_connect_queue"] == '["slack", "notion"]')

    first = await db.pop_next_pending_app_connect(1)
    check("pop_next_pending_app_connect returns the FIRST queued slug", first == "slack")
    check("...and rewrites the queue with that slug removed",
          conn.row["pending_app_connect_queue"] == '["notion"]')

    second = await db.pop_next_pending_app_connect(1)
    check("popping again returns the next (last) slug", second == "notion")
    check("...and the queue is now an empty JSON array", conn.row["pending_app_connect_queue"] == "[]")

    third = await db.pop_next_pending_app_connect(1)
    check("popping an empty queue returns None, doesn't raise", third is None)

    # NULL queue (never set) -> None, no crash.
    conn2 = UsersConn({"id": 2, "pending_app_connect_queue": None})
    install_fake_pool(conn2)
    check("a user with no queue at all pops None", await db.pop_next_pending_app_connect(2) is None)

    # Pre-migration graceful degrade: _has_column is False -> silent no-ops,
    # never crashes, matching every other optional column in this project.
    # _column_cache caches a True answer forever (db.py's own docstring), so
    # it must be cleared here or the earlier real columns' True answers
    # above would leak into this "un-migrated" scenario.
    db._column_cache.clear()
    conn3 = UsersConn({"id": 3}, has_column=False)
    install_fake_pool(conn3)
    await db.mark_apps_onboarding_asked(3)  # must not raise
    check("mark_apps_onboarding_asked no-ops pre-migration", "apps_onboarding_asked" not in conn3.row)
    await db.set_pending_app_connect_queue(3, ["gmail"])  # must not raise
    check("set_pending_app_connect_queue no-ops pre-migration", "pending_app_connect_queue" not in conn3.row)
    check("pop_next_pending_app_connect returns None pre-migration",
          await db.pop_next_pending_app_connect(3) is None)


# --- Part 2: cli.py _onboarding_complete_messages ---

async def part2_onboarding_apps_question():
    real_get_user_by_id = db.get_user_by_id
    real_mark_asked = db.mark_apps_onboarding_asked
    real_get_local_part = db.get_or_create_messa_email_local_part

    marked_calls = []

    async def fake_mark_asked(user_id):
        marked_calls.append(user_id)

    async def fake_get_local_part(user_id, name):
        return "jane"

    db.mark_apps_onboarding_asked = fake_mark_asked
    db.get_or_create_messa_email_local_part = fake_get_local_part

    try:
        # 2a: onboarding just completed, apps question never asked yet.
        async def fake_fresh_not_asked(user_id):
            return {
                "onboarding_step": "complete", "name": "Jane",
                "messa_email_local_part": "jane", "apps_onboarding_asked": False,
            }
        db.get_user_by_id = fake_fresh_not_asked

        user = config.UserContext(
            user_id=1, phone_number="+15550000000", name=None,
            onboarding_step="awaiting_name",  # turn started BEFORE completion -- onboarding_complete is False
        )
        messages = await cli._onboarding_complete_messages(user)
        check("apps question is appended to the reveal", any("what apps do you use day to day" in m for m in messages))
        check("apps question mentions concrete examples (Gmail, Slack, etc.)",
              any("Gmail" in m and "Slack" in m for m in messages))
        check("mark_apps_onboarding_asked is called exactly once", marked_calls == [1])

        # 2b: same shape, but apps_onboarding_asked already True -- never asked again.
        marked_calls.clear()

        async def fake_fresh_already_asked(user_id):
            return {
                "onboarding_step": "complete", "name": "Jane",
                "messa_email_local_part": "jane", "apps_onboarding_asked": True,
            }
        db.get_user_by_id = fake_fresh_already_asked

        user2 = config.UserContext(
            user_id=2, phone_number="+15550000001", name=None, onboarding_step="awaiting_name",
        )
        messages2 = await cli._onboarding_complete_messages(user2)
        check("already-asked user gets no apps question in the reveal",
              not any("what apps do you use day to day" in m for m in messages2))
        check("mark_apps_onboarding_asked is NOT called again", marked_calls == [])

        # 2c: onboarding wasn't the thing that just completed (already
        # complete at the top of the turn) -- short-circuits to [], the
        # apps question is never even considered.
        user3 = config.UserContext(user_id=3, phone_number="+15550000002", name="Jane", onboarding_step="complete")
        messages3 = await cli._onboarding_complete_messages(user3)
        check("a turn that starts already onboarded gets no reveal at all", messages3 == [])
    finally:
        db.get_user_by_id = real_get_user_by_id
        db.mark_apps_onboarding_asked = real_mark_asked
        db.get_or_create_messa_email_local_part = real_get_local_part


# --- Part 3: tools/integration_tools.py queue_app_connections ---

class FakeConnectedAccounts:
    def __init__(self):
        self.link_calls = []

    def link(self, *, user_id, auth_config_id, callback_url=None, **_kw):
        self.link_calls.append({"user_id": user_id, "auth_config_id": auth_config_id})
        return _Obj(id=f"ca_{auth_config_id}", redirect_url=f"https://composio.example/connect/{auth_config_id}")

    def list(self, *, user_ids=None, statuses=None, **_kw):
        return []


class FakeAuthConfigs:
    def __init__(self):
        self.created = []

    def list(self, *, toolkit_slug):
        return _Obj(items=[])

    def create(self, toolkit_slug, options):
        new_id = f"ac_{toolkit_slug}"
        self.created.append((toolkit_slug, options))
        return _Obj(id=new_id)


class FakeClient:
    def __init__(self):
        self.auth_configs = FakeAuthConfigs()
        self.connected_accounts = FakeConnectedAccounts()


def _make_user(user_id=1, channel="sms", phone="+15551234567"):
    return config.UserContext(user_id=user_id, phone_number=phone, channel=channel)


async def part3_queue_app_connections():
    it._auth_config_id_cache.clear()

    sent = []

    async def fake_send_message(number, content, media_url=None):
        sent.append((number, content))

    sendblue.send_message = fake_send_message
    it.send_message = fake_send_message

    created = []

    async def fake_create_request(user_id, toolkit_slug, connected_account_id):
        created.append((user_id, toolkit_slug, connected_account_id))
        return {"id": 1}

    db.create_app_connection_request = fake_create_request

    queued = []

    async def fake_set_queue(user_id, slugs):
        queued.append((user_id, list(slugs)))

    db.set_pending_app_connect_queue = fake_set_queue

    client = FakeClient()
    it._get_client = lambda: client

    user = _make_user(user_id=91)
    tools = it.build_integration_tools(user)
    queue_tool = next(t for t in tools if t.name == "queue_app_connections")

    # 3a: several apps, with a duplicate and mixed case -- dedupe/normalize,
    # first link sent immediately, the rest queued (in order, deduped).
    result = await queue_tool.coroutine(toolkit_slugs=["Slack", "notion", "slack", "Todoist"])
    check("the FIRST app's connect link is sent via send_connect_link (one Sendblue send)",
          len(sent) == 1 and "slack" in sent[0][1].lower())
    check("a connected_account_id request row is created for the first app only",
          created == [(91, "slack", "ca_ac_slack")])
    check("the rest (deduped, in order) are queued, not sent yet",
          queued == [(91, ["notion", "todoist"])])
    check("only ONE link was actually generated via Composio this call",
          len(client.connected_accounts.link_calls) == 1)
    check("the tool's own reply tells the model the rest are queued",
          "Queued the rest" in result and "notion" in result and "todoist" in result)

    # 3b: a single app -- no queueing at all, behaves like a plain connect.
    sent.clear()
    created.clear()
    queued.clear()
    it._auth_config_id_cache.clear()
    client2 = FakeClient()
    it._get_client = lambda: client2
    user2 = _make_user(user_id=92)
    tools2 = it.build_integration_tools(user2)
    queue_tool2 = next(t for t in tools2 if t.name == "queue_app_connections")
    result2 = await queue_tool2.coroutine(toolkit_slugs=["gmail"])
    check("a single-app list sends that one link", len(sent) == 1 and "gmail" in sent[0][1].lower())
    check("a single-app list never calls set_pending_app_connect_queue", queued == [])
    check("the reply for a single app doesn't mention queueing", "Queued the rest" not in result2)

    # 3c: empty/blank input -- nothing to queue, no Composio call at all.
    sent.clear()
    result3 = await queue_tool2.coroutine(toolkit_slugs=["", "   "])
    check("an empty/blank list is a no-op with a plain explanation, no crash",
          "Nothing to queue" in result3 and sent == [])

    it._auth_config_id_cache.clear()


# --- Part 4: server.py poll-loop hook ---

async def part4_poll_loop_advances_queue():
    from messa import server

    real_expire_stale = db.expire_stale_app_connection_requests
    real_get_pending = db.get_pending_app_connection_requests
    real_get_status = it.get_connection_status
    real_mark_connected = db.mark_app_connected
    real_pop_next = db.pop_next_pending_app_connect
    real_load_by_id = cli.load_user_context_by_id
    real_send_connect_link = it.send_connect_link
    real_sleep = asyncio.sleep
    real_send_message = sendblue.send_message
    real_confirmation = getattr(server, "_connection_confirmation_message", None)

    class _StopLoop(Exception):
        pass

    sent = []

    async def fake_send_message(number, content, media_url=None):
        sent.append((number, content))

    async def fake_confirmation(user_id, toolkit_slug, pretty_name):
        return f"Your {toolkit_slug} is connected!"

    try:
        async def fake_expire_stale():
            return None
        db.expire_stale_app_connection_requests = fake_expire_stale

        pending_rows = [
            {"id": 1, "user_id": 91, "phone_number": "+15551234567",
             "toolkit_slug": "slack", "connected_account_id": "ca_slack"},
        ]

        async def fake_get_pending():
            return pending_rows
        db.get_pending_app_connection_requests = fake_get_pending

        async def fake_get_status(connected_account_id):
            return "ACTIVE"
        it.get_connection_status = fake_get_status

        marked = []

        async def fake_mark_connected(req_id):
            marked.append(req_id)
        db.mark_app_connected = fake_mark_connected

        popped_for = []

        async def fake_pop_next(user_id):
            popped_for.append(user_id)
            return "notion"
        db.pop_next_pending_app_connect = fake_pop_next

        async def fake_load_by_id(user_id, channel="sms"):
            return config.UserContext(user_id=user_id, phone_number="+15551234567", channel=channel)
        cli.load_user_context_by_id = fake_load_by_id

        send_link_calls = []

        async def fake_send_connect_link(user, toolkit_slug):
            send_link_calls.append((user.user_id, toolkit_slug))
            return f"Sent the {toolkit_slug} connect link."
        it.send_connect_link = fake_send_connect_link

        sendblue.send_message = fake_send_message
        if real_confirmation is not None:
            server._connection_confirmation_message = fake_confirmation

        call_count = {"n": 0}

        async def sleep_once_then_stop(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] >= 1:
                raise _StopLoop()
        asyncio.sleep = sleep_once_then_stop

        try:
            await server._production_app_connection_poll_loop()
        except _StopLoop:
            pass

        check("the connection is marked connected as before (unaffected)", marked == [1])
        check("the confirmation text for the just-connected app still goes out",
              any("slack" in c[1].lower() for c in sent))
        check("pop_next_pending_app_connect is checked for the connecting user",
              popped_for == [91])
        check("a queued next app's link is sent to the SAME user via send_connect_link",
              send_link_calls == [(91, "notion")])

        # A user with nothing queued: pop returns None, send_connect_link is
        # never called, nothing else changes.
        sent.clear()
        marked.clear()
        popped_for.clear()
        send_link_calls.clear()
        call_count["n"] = 0

        async def fake_pop_next_none(user_id):
            popped_for.append(user_id)
            return None
        db.pop_next_pending_app_connect = fake_pop_next_none

        try:
            await server._production_app_connection_poll_loop()
        except _StopLoop:
            pass

        check("a user with an empty queue: pop_next is still checked",
              popped_for == [91])
        check("...but send_connect_link is never called (nothing queued)",
              send_link_calls == [])
        check("the normal connection flow (mark connected + confirmation) is fully unaffected",
              marked == [1] and any("slack" in c[1].lower() for c in sent))
    finally:
        db.expire_stale_app_connection_requests = real_expire_stale
        db.get_pending_app_connection_requests = real_get_pending
        it.get_connection_status = real_get_status
        db.mark_app_connected = real_mark_connected
        db.pop_next_pending_app_connect = real_pop_next
        cli.load_user_context_by_id = real_load_by_id
        it.send_connect_link = real_send_connect_link
        asyncio.sleep = real_sleep
        sendblue.send_message = real_send_message
        if real_confirmation is not None:
            server._connection_confirmation_message = real_confirmation


async def main() -> None:
    await part1_db_functions()
    await part2_onboarding_apps_question()
    await part3_queue_app_connections()
    await part4_poll_loop_advances_queue()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
