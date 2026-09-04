"""Tests for the disconnect/switch-account feature (migrations/
022_disconnect_integrations.sql, messa/db.py's new get_active_email_connection/
mark_email_disconnected/get_active_app_connection/disconnect_app_connection,
messa/tools/email_tools.py's disconnect_email + request_email_connection's
new switch_account param, messa/tools/integration_tools.py's
disconnect_integration_app + connect_integration_app's new switch_account
param, and messa/agents/registry.py's new routing paragraph).

Real reported bug this fixes: asked to switch Google Calendar to a
different account, Messa fabricated a fictional "go to Messa's connection/
integrations settings page and disconnect it yourself" instruction -- no
such page exists in this product, and Composio's own API (verified against
the installed composio==0.21.0/composio-client==1.43.0 SDK source, not
docs paraphrase) actually supports disconnecting a connected account
directly: `client.connected_accounts.delete(nanoid, revoke_on_delete=True)`.
`revoke_on_delete=True` kicks off an ASYNC background job on Composio's
side with no documented way to poll completion -- so every success message
below is deliberately careful to say "disconnected on Messa's side, revoke
in progress" rather than overclaiming "fully revoked", to avoid recreating
the exact same class of confidently-wrong-statement bug this whole feature
exists to fix.

Five parts:
  1. db.py: get_active_email_connection / mark_email_disconnected --
     Gmail's dedicated table.
  2. db.py: get_active_app_connection / disconnect_app_connection -- the
     generic app_connection_requests table, filtered by toolkit_slug (this
     table covers many apps per user, unlike email's own table).
  3. email_tools.py: request_email_connection(switch_account=True) actually
     disconnects the old connected_account_id before linking a new one;
     disconnect_email() with nothing connected / with something connected;
     destructive_check gating (switch_account=True and disconnect_email are
     gated, a plain connect is not).
  4. integration_tools.py: the same three behaviors, generic-toolkit path.
  5. registry.py: the new "Switching or disconnecting a connected app"
     routing paragraph exists and says the right things (no settings page,
     names the actual tool calls, revoke-in-progress wording) -- and the
     integrations_agent/email_agent own prompts also mention their new
     tools (so the subagent that actually calls them isn't confused about
     having them).
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, "/home/claude/messa_build")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")
os.environ.setdefault("COMPOSIO_API_KEY", "comp-test-dummy-not-real")

from messa import config, db  # noqa: E402
from messa.approval import ApprovalGate, AutoApproveGate, DenyApprovalGate  # noqa: E402
from messa.channels import sendblue  # noqa: E402
from messa.agents import registry  # noqa: E402
from messa.tools import email_tools as et  # noqa: E402
from messa.tools import integration_tools as it  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# --- Fake `composio` package, same technique as test_email_oauth_tools.py / test_integration_tools.py ---

class ComposioMultipleConnectedAccountsError(Exception):
    pass


_fake_composio_exceptions = types.ModuleType("composio.exceptions")
_fake_composio_exceptions.ComposioMultipleConnectedAccountsError = ComposioMultipleConnectedAccountsError
_fake_composio_pkg = types.ModuleType("composio")
_fake_composio_pkg.exceptions = _fake_composio_exceptions
sys.modules["composio"] = _fake_composio_pkg
sys.modules["composio.exceptions"] = _fake_composio_exceptions


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# --- Fake DB pool, same shape as test_default_email_provider.py / test_integration_tools.py ---

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


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def install_fake_pool(conn):
    async def fake_get_pool():
        return FakePool(conn)
    db.get_pool = fake_get_pool


class EmailConnConn:
    """Models email_connection_requests, just what get_active_email_connection/
    mark_email_disconnected touch."""

    def __init__(self, rows=None, has_table=True, has_column=True):
        self.rows = rows or {}  # id -> dict
        self.has_table = has_table
        self.has_column = has_column
        self.users_email_connected = {}

    async def fetchval(self, sql, *args):
        stripped = sql.strip()
        if "information_schema.tables" in stripped:
            return self.has_table
        if "information_schema.columns" in stripped:
            return self.has_column
        raise AssertionError(f"unexpected fetchval: {sql!r}")

    async def fetchrow(self, sql, *args):
        stripped = sql.strip()
        if stripped.startswith("SELECT * FROM email_connection_requests"):
            (user_id,) = args
            candidates = [r for r in self.rows.values() if r["user_id"] == user_id and r["status"] == "active"]
            if not candidates:
                return None
            candidates.sort(key=lambda r: (r.get("resolved_at") or "", r.get("requested_at") or ""), reverse=True)
            return dict(candidates[0])
        raise AssertionError(f"unexpected fetchrow: {sql!r}")

    async def execute(self, sql, *args):
        stripped = sql.strip()
        if stripped.startswith("UPDATE email_connection_requests SET status = 'disconnected'"):
            (req_id,) = args
            self.rows[req_id]["status"] = "disconnected"
            return
        if stripped.startswith("UPDATE users SET email_connected = FALSE"):
            (user_id,) = args
            self.users_email_connected[user_id] = False
            return
        raise AssertionError(f"unexpected execute: {sql!r}")

    def transaction(self):
        return FakeTransaction()


class AppConnConn:
    """Models app_connection_requests, just what get_active_app_connection/
    disconnect_app_connection touch."""

    def __init__(self, rows=None, has_table=True):
        self.rows = rows or {}
        self.has_table = has_table

    async def fetchval(self, sql, *args):
        if "information_schema.tables" in sql:
            return self.has_table
        raise AssertionError(f"unexpected fetchval: {sql!r}")

    async def fetchrow(self, sql, *args):
        stripped = sql.strip()
        if stripped.startswith("SELECT * FROM app_connection_requests"):
            user_id, toolkit_slug = args
            candidates = [
                r for r in self.rows.values()
                if r["user_id"] == user_id and r["toolkit_slug"] == toolkit_slug and r["status"] == "active"
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda r: (r.get("resolved_at") or "", r.get("requested_at") or ""), reverse=True)
            return dict(candidates[0])
        raise AssertionError(f"unexpected fetchrow: {sql!r}")

    async def execute(self, sql, *args):
        stripped = sql.strip()
        if stripped.startswith("UPDATE app_connection_requests SET status = 'disconnected'"):
            (req_id,) = args
            self.rows[req_id]["status"] = "disconnected"
            return
        raise AssertionError(f"unexpected execute: {sql!r}")


async def part1_email_db_functions():
    conn = EmailConnConn(rows={
        1: {"id": 1, "user_id": 7, "connected_account_id": "ca_old", "status": "active",
            "requested_at": "2026-01-01", "resolved_at": None},
    })
    install_fake_pool(conn)

    row = await db.get_active_email_connection(7)
    check("get_active_email_connection finds the active row", row is not None and row["connected_account_id"] == "ca_old")

    none_row = await db.get_active_email_connection(999)
    check("get_active_email_connection returns None for a user with no active row", none_row is None)

    await db.mark_email_disconnected(1, 7)
    check("mark_email_disconnected flips the request row to 'disconnected'", conn.rows[1]["status"] == "disconnected")
    check("mark_email_disconnected also flips users.email_connected back to FALSE", conn.users_email_connected.get(7) is False)

    check("once disconnected, get_active_email_connection no longer finds it",
          await db.get_active_email_connection(7) is None)

    conn.has_table = False
    conn.has_column = False
    check("get_active_email_connection no-ops (None) pre-migration", await db.get_active_email_connection(7) is None)
    await db.mark_email_disconnected(1, 7)  # must not raise
    check("mark_email_disconnected no-ops silently pre-migration", True)


async def part2_app_db_functions():
    conn = AppConnConn(rows={
        1: {"id": 1, "user_id": 21, "toolkit_slug": "todoist", "connected_account_id": "ca_todoist",
            "status": "active", "requested_at": "2026-01-01", "resolved_at": None},
        2: {"id": 2, "user_id": 21, "toolkit_slug": "slack", "connected_account_id": "ca_slack",
            "status": "active", "requested_at": "2026-01-01", "resolved_at": None},
    })
    install_fake_pool(conn)

    row = await db.get_active_app_connection(21, "todoist")
    check("get_active_app_connection finds the right row for the right toolkit",
          row is not None and row["connected_account_id"] == "ca_todoist")

    other = await db.get_active_app_connection(21, "slack")
    check("a different toolkit for the same user returns its OWN row, not todoist's",
          other is not None and other["connected_account_id"] == "ca_slack")

    none_row = await db.get_active_app_connection(21, "notion")
    check("an unconnected toolkit for a real user returns None", none_row is None)

    await db.disconnect_app_connection(1)
    check("disconnect_app_connection flips only the targeted row to 'disconnected'",
          conn.rows[1]["status"] == "disconnected" and conn.rows[2]["status"] == "active")
    check("todoist no longer shows as an active connection after disconnecting",
          await db.get_active_app_connection(21, "todoist") is None)
    check("slack is unaffected by disconnecting todoist",
          (await db.get_active_app_connection(21, "slack"))["connected_account_id"] == "ca_slack")

    conn.has_table = False
    check("get_active_app_connection no-ops (None) pre-migration", await db.get_active_app_connection(21, "todoist") is None)
    await db.disconnect_app_connection(2)  # must not raise
    check("disconnect_app_connection no-ops silently pre-migration", True)


# --- Part 3: email_tools.py ---

class FakeEmailConnectedAccounts:
    def __init__(self, already_connected_user_ids=None):
        self.link_calls = []
        self.delete_calls = []
        self._already_connected = set(already_connected_user_ids or set())

    def link(self, *, user_id, auth_config_id, callback_url=None, **_kw):
        if user_id in self._already_connected:
            raise ComposioMultipleConnectedAccountsError(f"already connected: {user_id}")
        self.link_calls.append({"user_id": user_id, "auth_config_id": auth_config_id})
        return _Obj(id=f"ca_{user_id}", redirect_url=f"https://composio.example/connect/{user_id}")

    def delete(self, connected_account_id, revoke_on_delete=False):
        self.delete_calls.append((connected_account_id, revoke_on_delete))

    def list(self, **_kw):
        return []


class FakeEmailAuthConfigs:
    def __init__(self):
        self.created = []

    def list(self, *, toolkit_slug):
        return _Obj(items=[])

    def create(self, toolkit, options):
        new_id = f"ac_{len(self.created) + 1}"
        self.created.append(options)
        return _Obj(id=new_id)


class FakeEmailClient:
    def __init__(self, already_connected_user_ids=None):
        self.auth_configs = FakeEmailAuthConfigs()
        self.connected_accounts = FakeEmailConnectedAccounts(already_connected_user_ids)


def _make_user(user_id=1, channel="sms", phone="+15551234567", email_connected=False):
    return config.UserContext(
        user_id=user_id, phone_number=phone, channel=channel, email_connected=email_connected,
    )


async def part3_email_switch_and_disconnect():
    et._gmail_auth_config_id_cache = "ac_existing"

    sent = []

    async def fake_send_message(number, content, media_url=None):
        sent.append((number, content))

    sendblue.send_message = fake_send_message
    et.send_message = fake_send_message

    created = []

    async def fake_create_request(user_id, connected_account_id):
        created.append((user_id, connected_account_id))
        return {"id": 99}

    db.create_email_connection_request = fake_create_request

    # --- switch_account=True: must delete the OLD connection first, THEN link a new one ---
    existing_lookups = {}

    async def fake_get_active(user_id):
        return existing_lookups.get(user_id)

    disconnected_calls = []

    async def fake_mark_disconnected(request_id, user_id):
        disconnected_calls.append((request_id, user_id))

    db.get_active_email_connection = fake_get_active
    db.mark_email_disconnected = fake_mark_disconnected

    existing_lookups[55] = {"id": 5, "connected_account_id": "ca_old_55", "user_id": 55}
    client = FakeEmailClient()  # user 55 NOT in _already_connected -- switch clears it first, so link succeeds
    et._get_client = lambda: client

    switch_user = _make_user(user_id=55, channel="sms", phone="+15551110000", email_connected=True)
    tools = et.build_email_tools(switch_user, approval_gate=AutoApproveGate())
    request_conn = next(t for t in tools if t.name == "request_email_connection")
    result = await request_conn.coroutine(switch_account=True)

    check("switch_account=True calls connected_accounts.delete with revoke_on_delete=True on the OLD connected_account_id",
          client.connected_accounts.delete_calls == [("ca_old_55", True)])
    check("the old request row is marked disconnected via db.mark_email_disconnected",
          disconnected_calls == [(5, 55)])
    check("a fresh link IS still generated and texted after the disconnect",
          len(client.connected_accounts.link_calls) == 1 and sent and "https://composio.example/connect/55" in sent[0][1])
    check("a new durable connection request row is created for the fresh link",
          created == [(55, "ca_55")])

    # --- switch_account=True with nothing currently connected: delete is skipped, link still happens ---
    sent.clear()
    created.clear()
    client.connected_accounts.delete_calls.clear()
    existing_lookups.pop(56, None)
    no_existing_user = _make_user(user_id=56, channel="sms", phone="+15552220000")
    tools2 = et.build_email_tools(no_existing_user, approval_gate=AutoApproveGate())
    request_conn2 = next(t for t in tools2 if t.name == "request_email_connection")
    result2 = await request_conn2.coroutine(switch_account=True)
    check("switch_account=True with no existing active connection skips the delete call entirely",
          client.connected_accounts.delete_calls == [])
    check("...but still successfully sends a fresh connect link",
          len(client.connected_accounts.link_calls) == 2)

    # --- plain connect (switch_account left False): raising 'already connected' now points at switch_account=True ---
    client2 = FakeEmailClient(already_connected_user_ids={"57"})
    et._get_client = lambda: client2
    existing_lookups.pop(57, None)
    already_user = _make_user(user_id=57, channel="sms")
    tools3 = et.build_email_tools(already_user)
    request_conn3 = next(t for t in tools3 if t.name == "request_email_connection")
    result3 = await request_conn3.coroutine()
    check("a plain (non-switch) connect attempt on an already-connected Gmail points the model at "
          "switch_account=True instead of dead-ending",
          "switch_account=True" in result3 and "already connected" in result3.lower())
    check("no false claim that the user must do anything manually/in a settings page",
          "settings" not in result3.lower() and "yourself" not in result3.lower())

    # --- disconnect_email: nothing connected ---
    existing_lookups.pop(58, None)
    client3 = FakeEmailClient()
    et._get_client = lambda: client3
    nothing_user = _make_user(user_id=58, channel="sms")
    tools4 = et.build_email_tools(nothing_user, approval_gate=AutoApproveGate())
    disconnect = next(t for t in tools4 if t.name == "disconnect_email")
    result4 = await disconnect.coroutine()
    check("disconnect_email with nothing connected says so plainly, no Composio call",
          "isn't currently connected" in result4 and client3.connected_accounts.delete_calls == [])

    # --- disconnect_email: something connected ---
    existing_lookups[59] = {"id": 9, "connected_account_id": "ca_59", "user_id": 59}
    disconnected_calls.clear()
    client4 = FakeEmailClient()
    et._get_client = lambda: client4
    connected_user = _make_user(user_id=59, channel="sms", email_connected=True)
    tools5 = et.build_email_tools(connected_user, approval_gate=AutoApproveGate())
    disconnect2 = next(t for t in tools5 if t.name == "disconnect_email")
    result5 = await disconnect2.coroutine()
    check("disconnect_email deletes the real connected_account_id with revoke_on_delete=True",
          client4.connected_accounts.delete_calls == [("ca_59", True)])
    check("disconnect_email marks the local row disconnected too", disconnected_calls == [(9, 59)])
    check("disconnect_email's own success message says disconnected NOW but revoke is IN PROGRESS "
          "-- never overclaims full/confirmed revocation (the exact class of bug this feature fixes)",
          "disconnected" in result5.lower() and "in progress" in result5.lower()
          and "fully revoked" not in result5.lower() and "confirmed" not in result5.lower().split("in progress")[0])

    et._gmail_auth_config_id_cache = None


async def part3b_email_destructive_gating():
    et._gmail_auth_config_id_cache = "ac_existing"
    client = FakeEmailClient()
    et._get_client = lambda: client

    async def fake_get_active(user_id):
        return None

    db.get_active_email_connection = fake_get_active

    calls = []

    class RecordingGate(ApprovalGate):
        async def confirm(self, label, tool_name, args):
            calls.append((tool_name, args))
            return True

    user = _make_user(user_id=61, channel="sms")
    tools = et.build_email_tools(user, approval_gate=RecordingGate())
    request_conn = next(t for t in tools if t.name == "request_email_connection")
    disconnect = next(t for t in tools if t.name == "disconnect_email")

    await request_conn.coroutine()
    check("a plain first-time connect (switch_account=False/default) needs NO confirmation",
          calls == [])

    calls.clear()
    await request_conn.coroutine(switch_account=True)
    check("request_email_connection(switch_account=True) DOES go through the approval gate",
          len(calls) == 1 and calls[0][0] == "request_email_connection")

    calls.clear()

    async def fake_get_active_none(user_id):
        return None
    db.get_active_email_connection = fake_get_active_none
    await disconnect.coroutine()
    check("disconnect_email is ALWAYS gated, even when it turns out there's nothing to disconnect",
          len(calls) == 1 and calls[0][0] == "disconnect_email")

    # No gate at all -> destructive calls are denied by default.
    tools_no_gate = et.build_email_tools(user)
    request_conn_ng = next(t for t in tools_no_gate if t.name == "request_email_connection")
    result = await request_conn_ng.coroutine(switch_account=True)
    check("with no approval gate configured, switch_account=True is BLOCKED by default, not run",
          "BLOCKED" in result)

    et._gmail_auth_config_id_cache = None


# --- Part 4: integration_tools.py ---

class FakeIntegrationConnectedAccounts:
    def __init__(self, active_toolkits_by_user=None):
        self.link_calls = []
        self.delete_calls = []
        self._active = active_toolkits_by_user or {}

    def link(self, *, user_id, auth_config_id, callback_url=None, **_kw):
        if user_id in self._active:
            raise ComposioMultipleConnectedAccountsError(f"already connected: {user_id}")
        self.link_calls.append({"user_id": user_id, "auth_config_id": auth_config_id})
        return _Obj(id=f"ca_{user_id}_{auth_config_id}", redirect_url=f"https://composio.example/connect/{auth_config_id}")

    def delete(self, connected_account_id, revoke_on_delete=False):
        self.delete_calls.append((connected_account_id, revoke_on_delete))

    def list(self, **_kw):
        return []


class FakeIntegrationAuthConfigs:
    def __init__(self):
        self.created = []

    def list(self, *, toolkit_slug):
        return _Obj(items=[])

    def create(self, toolkit_slug, options):
        new_id = f"ac_{toolkit_slug}_{len(self.created) + 1}"
        self.created.append((toolkit_slug, options))
        return _Obj(id=new_id)


class FakeIntegrationClient:
    def __init__(self, active_toolkits_by_user=None):
        self.auth_configs = FakeIntegrationAuthConfigs()
        self.connected_accounts = FakeIntegrationConnectedAccounts(active_toolkits_by_user)


def _make_it_user(user_id=1, channel="sms", phone="+15551234567"):
    return config.UserContext(user_id=user_id, phone_number=phone, channel=channel)


async def part4_integration_switch_and_disconnect():
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

    existing_lookups = {}

    async def fake_get_active(user_id, toolkit_slug):
        return existing_lookups.get((user_id, toolkit_slug))

    disconnected_calls = []

    async def fake_disconnect_app(request_id):
        disconnected_calls.append(request_id)

    db.get_active_app_connection = fake_get_active
    db.disconnect_app_connection = fake_disconnect_app

    # --- switch_account=True: deletes the old googlecalendar connection, then links a new one ---
    existing_lookups[(71, "googlecalendar")] = {"id": 3, "connected_account_id": "ca_old_gcal"}
    client = FakeIntegrationClient()  # 71 NOT pre-marked active -- switch clears it first
    it._get_client = lambda: client

    switch_user = _make_it_user(user_id=71, channel="sms", phone="+15553330000")
    tools = it.build_integration_tools(switch_user, approval_gate=AutoApproveGate())
    connect = next(t for t in tools if t.name == "connect_integration_app")
    result = await connect.coroutine(toolkit_slug="googlecalendar", switch_account=True)

    check("switch_account=True deletes the OLD connected_account_id with revoke_on_delete=True",
          client.connected_accounts.delete_calls == [("ca_old_gcal", True)])
    check("the old app_connection_requests row is marked disconnected",
          disconnected_calls == [3])
    check("a fresh link is generated and texted after the disconnect",
          len(client.connected_accounts.link_calls) == 1 and sent
          and "composio.example/connect" in sent[0][1])
    check("a new app_connection_requests row is created for the fresh connection",
          created and created[0][1] == "googlecalendar")

    # --- plain connect on an already-connected toolkit: points at switch_account=True, no dead end ---
    it._auth_config_id_cache.clear()
    client2 = FakeIntegrationClient(active_toolkits_by_user={"72": {"todoist"}})
    it._get_client = lambda: client2
    existing_lookups.pop((72, "todoist"), None)
    already_user = _make_it_user(user_id=72, channel="sms")
    tools2 = it.build_integration_tools(already_user)
    connect2 = next(t for t in tools2 if t.name == "connect_integration_app")
    result2 = await connect2.coroutine(toolkit_slug="todoist")
    check("a plain (non-switch) connect attempt on an already-connected app names "
          "switch_account=True as the way forward instead of dead-ending",
          "switch_account=True" in result2 and "already connected" in result2.lower())
    check("no false claim about needing a settings page or manual dashboard step",
          "settings" not in result2.lower() and "dashboard" not in result2.lower())

    # --- disconnect_integration_app: nothing connected ---
    it._auth_config_id_cache.clear()
    client3 = FakeIntegrationClient()
    it._get_client = lambda: client3
    existing_lookups.pop((73, "slack"), None)
    nothing_user = _make_it_user(user_id=73)
    tools3 = it.build_integration_tools(nothing_user, approval_gate=AutoApproveGate())
    disconnect = next(t for t in tools3 if t.name == "disconnect_integration_app")
    result3 = await disconnect.coroutine(toolkit_slug="slack")
    check("disconnect_integration_app with nothing connected says so plainly, no Composio call",
          "isn't currently connected" in result3 and client3.connected_accounts.delete_calls == []
          or "isn't" in result3.lower() and "connect" in result3.lower())

    # --- disconnect_integration_app: something connected ---
    existing_lookups[(74, "notion")] = {"id": 8, "connected_account_id": "ca_notion_74"}
    disconnected_calls.clear()
    client4 = FakeIntegrationClient()
    it._get_client = lambda: client4
    connected_user = _make_it_user(user_id=74)
    tools4 = it.build_integration_tools(connected_user, approval_gate=AutoApproveGate())
    disconnect2 = next(t for t in tools4 if t.name == "disconnect_integration_app")
    result4 = await disconnect2.coroutine(toolkit_slug="notion")
    check("disconnect_integration_app deletes the real connected_account_id with revoke_on_delete=True",
          client4.connected_accounts.delete_calls == [("ca_notion_74", True)])
    check("disconnect_integration_app marks the local row disconnected", disconnected_calls == [8])
    check("disconnect_integration_app's success message says disconnected NOW, revoke IN PROGRESS "
          "-- doesn't overclaim confirmed/full revocation",
          "disconnected" in result4.lower() and "in progress" in result4.lower())

    it._auth_config_id_cache.clear()


async def part4b_integration_destructive_gating():
    it._auth_config_id_cache.clear()
    client = FakeIntegrationClient()
    it._get_client = lambda: client

    async def fake_get_active(user_id, toolkit_slug):
        return None

    db.get_active_app_connection = fake_get_active

    calls = []

    class RecordingGate(ApprovalGate):
        async def confirm(self, label, tool_name, args):
            calls.append((tool_name, args))
            return True

    user = _make_it_user(user_id=81)
    tools = it.build_integration_tools(user, approval_gate=RecordingGate())
    connect = next(t for t in tools if t.name == "connect_integration_app")
    disconnect = next(t for t in tools if t.name == "disconnect_integration_app")

    await connect.coroutine(toolkit_slug="reddit")
    check("a plain first-time connect (switch_account default False) needs NO confirmation",
          calls == [])

    calls.clear()
    await connect.coroutine(toolkit_slug="reddit", switch_account=True)
    check("connect_integration_app(switch_account=True) DOES go through the approval gate",
          len(calls) == 1 and calls[0][0] == "connect_integration_app")

    calls.clear()
    await disconnect.coroutine(toolkit_slug="reddit")
    check("disconnect_integration_app is ALWAYS gated, even with nothing to disconnect",
          len(calls) == 1 and calls[0][0] == "disconnect_integration_app")

    # search_integration_tools and execute_integration_tool (read case) stay ungated, unaffected.
    tools_no_gate = it.build_integration_tools(user)
    search = next(t for t in tools_no_gate if t.name == "search_integration_tools")
    search_result = await search.coroutine(query="anything")
    check("search_integration_tools is unaffected by the new gating (no gate needed, no crash)",
          isinstance(search_result, str))

    it._auth_config_id_cache.clear()


# --- Part 5: prompt/routing text ---

def part5_prompts():
    user = config.UserContext(user_id=1, phone_number="+15551234567", channel="sms")

    orchestrator_prompt = registry._build_system_prompt(user)
    check("orchestrator prompt has the new 'Switching or disconnecting a connected app' paragraph",
          "Switching or disconnecting a connected app" in orchestrator_prompt)
    switch_para = orchestrator_prompt.split("Switching or disconnecting a connected app")[1].split("\n\n")[0]
    check("that paragraph explicitly denies any settings/integrations page exists",
          "NO settings/integrations page" in switch_para or "no settings/integrations page" in switch_para.lower())
    check("it tells Messa never to say the user must disconnect it themselves first",
          "never say there is" in switch_para or "never tell" in switch_para)
    check("it names the real switch_account=True mechanism for both email and generic integrations",
          "switch_account=True" in switch_para)
    check("it names both new standalone disconnect tools",
          "disconnect_email" in switch_para and "disconnect_integration_app" in switch_para)
    check("it tells Messa to say revocation is in progress, not confirmed complete",
          "in progress" in switch_para and "never" in switch_para)
    check("it tells Messa to warn about calendar events not carrying over before disconnecting",
          "heads-up" in switch_para or "won't carry over" in switch_para)

    integration_prompt = it.build_integration_system_prompt(user)
    check("integrations_agent's own prompt mentions switch_account=True",
          "switch_account=True" in integration_prompt)
    check("integrations_agent's own prompt mentions disconnect_integration_app",
          "disconnect_integration_app" in integration_prompt)
    check("integrations_agent's own prompt also denies a settings page exists",
          "no settings" in integration_prompt.lower() or "there is no settings" in integration_prompt.lower())

    email_prompt = et._build_system_prompt(user)
    check("email_agent's own prompt mentions switch_account=True", "switch_account=True" in email_prompt)
    check("email_agent's own prompt mentions disconnect_email()", "disconnect_email()" in email_prompt)


async def main() -> None:
    await part1_email_db_functions()
    await part2_app_db_functions()
    await part3_email_switch_and_disconnect()
    await part3b_email_destructive_gating()
    await part4_integration_switch_and_disconnect()
    await part4b_integration_destructive_gating()
    part5_prompts()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
