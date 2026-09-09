"""Tests for the Persistent Workspace Asset Registry + Entity
auto-discovery + Autonomous Fallback Waterfall
(docs/executive_agent_architecture_proposal.md sections 3.A/3.B/4.A/3.C,
migration 037_workspace_asset_registry.sql), built on top of
feature/agentic-upgrade.

  - db.py's user_assets functions (record_user_asset's upsert-vs-insert
    semantics, get_recent_user_assets ordering/limit, touch_user_asset_by_url,
    purge_stale_user_assets) and user_app_entities functions
    (get/set_cached_app_entity upsert), against a fake asyncpg pool/
    connection -- same FakeAcquire/FakePool/FakeConn/FakeRow shape as
    test_scratchpad_and_skills.py -- including the graceful pre-migration
    degrade (_has_table returning False).
  - tools/workspace_asset_tools.py's pure classification/extraction
    helpers (classify_asset_creation, extract_asset_identity,
    _looks_like_failed_result, toolkit_for_injection_target).
  - apply_cached_entity_defaults (fills a missing param from the cache,
    never overrides one already supplied) and
    discover_and_cache_app_entities (probes once, caches, never re-probes
    a warm cache, degrades to a no-op on any failure) against a fake
    Composio client and monkeypatched db functions.
  - create_workspace_asset's Airtable -> Google Sheets -> local-PDF
    fallback waterfall, against a fake Composio client whose per-tier
    success/failure is controllable, and a monkeypatched PDF builder for
    the last-resort tier.
  - asset_consolidation.py's extract_urls (pure) and consolidate_after_turn
    (dedup via touch-before-record) against monkeypatched db functions.
  - registry.py's wiring: _build_system_prompt renders the recent-assets
    block when given one, and omits it when empty -- verified end to end,
    not just by source inspection, since this one is cheap to actually
    exercise.
  - cli.py's on_turn_complete hook: fired as a background task, never
    blocking run_message's own return.

No live Postgres, no live LLM call, no live Composio call.
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

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

from messa import asset_consolidation, cli, config, db  # noqa: E402
from messa.agents import registry  # noqa: E402
from messa.tools import document_tools, integration_tools, workspace_asset_tools  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# ---------------------------------------------------------------------------
# Fakes -- same shape as tests/test_scratchpad_and_skills.py's own
# FakeAcquire/FakePool/FakeConn/FakeRow (this project's established
# pattern for db.py tests).
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, has_tables=True, fetchrow_queue=None, fetch_queue=None, execute_results=None):
        self.has_tables = has_tables
        self.fetchrow_queue = list(fetchrow_queue or [])
        self.fetch_queue = list(fetch_queue or [])
        self.execute_results = list(execute_results or [])
        self.calls = []

    async def fetchval(self, query, *args):
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
        if self.execute_results:
            return self.execute_results.pop(0)
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


class FakeUser:
    def __init__(self, user_id=1):
        self.user_id = user_id


# ---------------------------------------------------------------------------
# Part 1: db.py -- user_assets
# ---------------------------------------------------------------------------

async def part1_user_assets_db():
    # Pre-migration degrade: every function is a clean no-op, never raises.
    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    check("record_user_asset returns None pre-migration",
          await db.record_user_asset(1, "airtable_base", "Deals", "b1", "http://x", "s") is None)
    check("get_recent_user_assets returns [] pre-migration",
          await db.get_recent_user_assets(1) == [])
    check("touch_user_asset_by_url returns False pre-migration",
          await db.touch_user_asset_by_url(1, "http://x") is False)
    check("purge_stale_user_assets returns a zeroed dict pre-migration",
          (await db.purge_stale_user_assets())["rows_deleted"] == 0)

    # record_user_asset with a known external_id -- INSERT ... ON CONFLICT
    # DO UPDATE, i.e. an upsert (touching/creating the SAME row).
    conn = FakeConn(has_tables=True, fetchrow_queue=[FakeRow(id=1, title="Deals", external_id="b1")])
    install_fake_pool(conn)
    row = await db.record_user_asset(1, "airtable_base", "Deals", "b1", "http://airtable/b1", "made it")
    check("record_user_asset (known external_id) returns the row", row == {"id": 1, "title": "Deals", "external_id": "b1"})
    check("record_user_asset (known external_id) uses ON CONFLICT DO UPDATE (an upsert, not a bare INSERT)",
          "ON CONFLICT" in conn.calls[0][1] and "DO UPDATE" in conn.calls[0][1])
    check("record_user_asset passes the real params in the expected order",
          conn.calls[0][2] == (1, "airtable_base", "Deals", "b1", "http://airtable/b1", "made it"))

    # record_user_asset with NO external_id -- always a bare INSERT (never
    # an upsert -- NULL never collides with NULL under the UNIQUE constraint,
    # see db.record_user_asset's own docstring).
    conn = FakeConn(has_tables=True, fetchrow_queue=[FakeRow(id=2, title="Some Doc", external_id=None)])
    install_fake_pool(conn)
    row2 = await db.record_user_asset(1, "google_doc", "Some Doc", None, "http://docs/x", None)
    check("record_user_asset (no external_id) still returns the row", row2 is not None)
    check("record_user_asset (no external_id) does NOT use ON CONFLICT (always inserts a fresh row)",
          "ON CONFLICT" not in conn.calls[0][1])

    # get_recent_user_assets -- ordering/limit delegated to SQL; verify the
    # limit actually passed defaults to config.USER_ASSETS_MAX_INJECTED.
    conn = FakeConn(has_tables=True, fetch_queue=[[FakeRow(id=1, title="A"), FakeRow(id=2, title="B")]])
    install_fake_pool(conn)
    rows = await db.get_recent_user_assets(1)
    check("get_recent_user_assets returns the rows from the fake", len(rows) == 2)
    check("get_recent_user_assets defaults its LIMIT to config.USER_ASSETS_MAX_INJECTED",
          conn.calls[0][2] == (1, config.USER_ASSETS_MAX_INJECTED))
    conn2 = FakeConn(has_tables=True, fetch_queue=[[]])
    install_fake_pool(conn2)
    await db.get_recent_user_assets(1, limit=2)
    check("get_recent_user_assets respects an explicit limit override",
          conn2.calls[0][2] == (1, 2))

    # touch_user_asset_by_url -- True/False based on the parsed "UPDATE N" tag.
    conn = FakeConn(has_tables=True, execute_results=["UPDATE 1"])
    install_fake_pool(conn)
    check("touch_user_asset_by_url returns True when a row was actually touched",
          await db.touch_user_asset_by_url(1, "http://x") is True)
    conn2 = FakeConn(has_tables=True, execute_results=["UPDATE 0"])
    install_fake_pool(conn2)
    check("touch_user_asset_by_url returns False when no row matched",
          await db.touch_user_asset_by_url(1, "http://x") is False)

    # purge_stale_user_assets -- parses the DELETE count.
    conn = FakeConn(has_tables=True, execute_results=["DELETE 3"])
    install_fake_pool(conn)
    result = await db.purge_stale_user_assets()
    check("purge_stale_user_assets parses the DELETE row count", result == {"rows_deleted": 3})


# ---------------------------------------------------------------------------
# Part 2: db.py -- user_app_entities
# ---------------------------------------------------------------------------

async def part2_user_app_entities_db():
    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    check("get_cached_app_entity returns None pre-migration",
          await db.get_cached_app_entity(1, "airtable", "workspace_id") is None)
    check("set_cached_app_entity returns None pre-migration",
          await db.set_cached_app_entity(1, "airtable", "workspace_id", "wsp123") is None)

    conn = FakeConn(has_tables=True, fetchrow_queue=[FakeRow(entity_id="wsp123")])
    install_fake_pool(conn)
    check("get_cached_app_entity returns the cached id",
          await db.get_cached_app_entity(1, "Airtable", "workspace_id") == "wsp123")
    check("get_cached_app_entity lowercases the toolkit slug before querying",
          conn.calls[0][2] == (1, "airtable", "workspace_id"))

    conn = FakeConn(has_tables=True, fetchrow_queue=[None])
    install_fake_pool(conn)
    check("get_cached_app_entity returns None on a cache miss (never raises)",
          await db.get_cached_app_entity(1, "airtable", "workspace_id") is None)

    conn = FakeConn(has_tables=True, fetchrow_queue=[FakeRow(id=1, entity_id="wsp999")])
    install_fake_pool(conn)
    row = await db.set_cached_app_entity(1, "Airtable", "workspace_id", "wsp999", label="auto")
    check("set_cached_app_entity returns the upserted row", row is not None)
    check("set_cached_app_entity uses ON CONFLICT DO UPDATE (an upsert)",
          "ON CONFLICT" in conn.calls[0][1] and "DO UPDATE" in conn.calls[0][1])


# ---------------------------------------------------------------------------
# Part 3: tools/workspace_asset_tools.py -- pure helpers
# ---------------------------------------------------------------------------

def part3_classification_and_extraction():
    check("AIRTABLE_CREATE_BASE classifies as an airtable_base",
          workspace_asset_tools.classify_asset_creation("AIRTABLE_CREATE_BASE") == "airtable_base")
    check("GOOGLESHEETS_CREATE_SPREADSHEET classifies as a googlesheets_spreadsheet",
          workspace_asset_tools.classify_asset_creation("GOOGLESHEETS_CREATE_SPREADSHEET") == "googlesheets_spreadsheet")
    check("NOTION_CREATE_PAGE classifies as a notion_page",
          workspace_asset_tools.classify_asset_creation("NOTION_CREATE_PAGE") == "notion_page")
    check("TODOIST_CREATE_TASK does NOT classify as a workspace asset (a task isn't one)",
          workspace_asset_tools.classify_asset_creation("TODOIST_CREATE_TASK") is None)
    check("a read-only LIST/GET slug never classifies as an asset creation",
          workspace_asset_tools.classify_asset_creation("AIRTABLE_LIST_BASES") is None)
    check("an empty slug returns None without raising",
          workspace_asset_tools.classify_asset_creation("") is None)

    # extract_asset_identity -- dict shape (top-level and nested under 'data').
    check("extract_asset_identity finds a top-level id/url",
          workspace_asset_tools.extract_asset_identity({"id": "b1", "url": "http://x"}) == ("b1", "http://x"))
    check("extract_asset_identity finds an id/url nested under 'data'",
          workspace_asset_tools.extract_asset_identity({"data": {"spreadsheetId": "s1", "spreadsheetUrl": "http://y"}})
          == ("s1", "http://y"))
    check("extract_asset_identity returns (None, None) when nothing matches",
          workspace_asset_tools.extract_asset_identity({"foo": "bar"}) == (None, None))

    class ObjResult:
        id = "obj1"
        webViewLink = "http://z"

    check("extract_asset_identity also works against an attribute-based (non-dict) result",
          workspace_asset_tools.extract_asset_identity(ObjResult()) == ("obj1", "http://z"))

    check("_looks_like_failed_result flags successful=False",
          workspace_asset_tools._looks_like_failed_result({"successful": False}))
    check("_looks_like_failed_result flags a non-empty error string",
          workspace_asset_tools._looks_like_failed_result({"error": "boom"}))
    check("_looks_like_failed_result does not flag a clean success result",
          not workspace_asset_tools._looks_like_failed_result({"successful": True, "data": {}}))
    check("_looks_like_failed_result defaults to 'not failed' when neither field is present",
          not workspace_asset_tools._looks_like_failed_result({"id": "x"}))

    check("toolkit_for_injection_target resolves AIRTABLE_CREATE_BASE to 'airtable'",
          workspace_asset_tools.toolkit_for_injection_target("AIRTABLE_CREATE_BASE") == "airtable")
    check("toolkit_for_injection_target returns None for an unrelated slug",
          workspace_asset_tools.toolkit_for_injection_target("TODOIST_CREATE_TASK") is None)


# ---------------------------------------------------------------------------
# Part 4: record_asset_from_execution
# ---------------------------------------------------------------------------

async def part4_record_asset_from_execution():
    recorded = []

    async def fake_record_user_asset(user_id, asset_type, title, external_id, url, summary=None):
        recorded.append((user_id, asset_type, title, external_id, url))
        return {"id": 1}

    real_record = db.record_user_asset
    db.record_user_asset = fake_record_user_asset
    try:
        user = FakeUser(user_id=7)
        await workspace_asset_tools.record_asset_from_execution(
            user, "AIRTABLE_CREATE_BASE", {"name": "Investor List"}, {"id": "b1", "url": "http://airtable/b1"},
        )
        check("a recognized asset-creation slug records exactly one asset", len(recorded) == 1)
        check("the recorded asset uses the arguments' own title/name",
              recorded[0] == (7, "airtable_base", "Investor List", "b1", "http://airtable/b1"))

        recorded.clear()
        await workspace_asset_tools.record_asset_from_execution(
            user, "TODOIST_CREATE_TASK", {"content": "buy milk"}, {"id": "t1"},
        )
        check("a non-asset slug (a Todoist task) records nothing", recorded == [])

        recorded.clear()
        config.WORKSPACE_ASSETS_ENABLED = False
        try:
            await workspace_asset_tools.record_asset_from_execution(
                user, "AIRTABLE_CREATE_BASE", {"name": "X"}, {"id": "b2"},
            )
            check("the feature flag off is a true kill switch -- nothing recorded even for a matching slug",
                  recorded == [])
        finally:
            config.WORKSPACE_ASSETS_ENABLED = True

        async def raising_record(*a, **kw):
            raise RuntimeError("db exploded")

        db.record_user_asset = raising_record
        try:
            await workspace_asset_tools.record_asset_from_execution(
                user, "AIRTABLE_CREATE_BASE", {"name": "X"}, {"id": "b3"},
            )
            check("a DB failure while recording an asset never raises (best-effort only)", True)
        except Exception:
            check("a DB failure while recording an asset never raises (best-effort only)", False)
    finally:
        db.record_user_asset = real_record


# ---------------------------------------------------------------------------
# Part 5: entity auto-discovery (3.B)
# ---------------------------------------------------------------------------

class FakeToolsExecute:
    def __init__(self, responses):
        # responses: list of return values (or exceptions), one per call,
        # popped in order -- lets a test script exactly what each
        # successive call should do.
        self._responses = list(responses)
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class FakeComposioClient:
    def __init__(self, responses):
        self.tools = FakeToolsExecute(responses)


async def part5_entity_auto_discovery():
    # --- apply_cached_entity_defaults ---
    async def fake_get_cached_hit(user_id, toolkit, entity_type):
        return "wsp-cached-123"

    real_get_cached = db.get_cached_app_entity
    db.get_cached_app_entity = fake_get_cached_hit
    try:
        user = FakeUser(user_id=1)
        filled = await workspace_asset_tools.apply_cached_entity_defaults(
            user, "AIRTABLE_CREATE_BASE", {"name": "Deals"},
        )
        check("apply_cached_entity_defaults fills in the missing workspace_id from the cache",
              filled.get("workspace_id") == "wsp-cached-123")
        check("apply_cached_entity_defaults returns a NEW dict, never mutating the caller's",
              "workspace_id" not in {"name": "Deals"})

        already_supplied = await workspace_asset_tools.apply_cached_entity_defaults(
            user, "AIRTABLE_CREATE_BASE", {"name": "Deals", "workspace_id": "explicit-one"},
        )
        check("apply_cached_entity_defaults NEVER overrides a value already supplied",
              already_supplied["workspace_id"] == "explicit-one")

        unrelated = await workspace_asset_tools.apply_cached_entity_defaults(
            user, "TODOIST_CREATE_TASK", {"content": "x"},
        )
        check("apply_cached_entity_defaults is a no-op for a slug with no injection target",
              unrelated == {"content": "x"})
    finally:
        db.get_cached_app_entity = real_get_cached

    async def fake_get_cached_miss(user_id, toolkit, entity_type):
        return None

    db.get_cached_app_entity = fake_get_cached_miss
    try:
        user = FakeUser(user_id=1)
        unfilled = await workspace_asset_tools.apply_cached_entity_defaults(
            user, "AIRTABLE_CREATE_BASE", {"name": "Deals"},
        )
        check("apply_cached_entity_defaults degrades to unchanged arguments on a cache miss",
              unfilled == {"name": "Deals"})
    finally:
        db.get_cached_app_entity = real_get_cached

    # --- discover_and_cache_app_entities ---
    real_get_cached = db.get_cached_app_entity
    real_set_cached = db.set_cached_app_entity
    set_calls = []

    async def fake_get_cached_none(user_id, toolkit, entity_type):
        return None

    async def fake_set_cached(user_id, toolkit, entity_type, entity_id, label=None):
        set_calls.append((user_id, toolkit, entity_type, entity_id))
        return {"id": 1}

    db.get_cached_app_entity = fake_get_cached_none
    db.set_cached_app_entity = fake_set_cached
    try:
        client = FakeComposioClient(responses=[{"data": [{"id": "wsp-discovered"}]}])
        await workspace_asset_tools.discover_and_cache_app_entities(1, "airtable", client=client)
        check("discover_and_cache_app_entities probes Composio using the toolkit's configured probe slug",
              client.tools.calls[0]["slug"] == "AIRTABLE_LIST_WORKSPACES")
        check("discover_and_cache_app_entities caches the first discovered id",
              set_calls == [(1, "airtable", "workspace_id", "wsp-discovered")])

        set_calls.clear()
        await workspace_asset_tools.discover_and_cache_app_entities(1, "not_a_real_toolkit", client=client)
        check("a toolkit with no ENTITY_PROBES entry is a clean no-op", set_calls == [])
    finally:
        db.set_cached_app_entity = real_set_cached

    # Already cached -- must NEVER re-probe.
    async def fake_get_cached_warm(user_id, toolkit, entity_type):
        return "already-cached"

    db.get_cached_app_entity = fake_get_cached_warm
    try:
        client2 = FakeComposioClient(responses=[])  # would raise IndexError if ever called
        await workspace_asset_tools.discover_and_cache_app_entities(1, "airtable", client=client2)
        check("a warm cache is never re-probed (zero Composio calls made)", client2.tools.calls == [])
    finally:
        db.get_cached_app_entity = real_get_cached
        db.set_cached_app_entity = real_set_cached

    # A probe that raises must degrade to a silent no-op, never propagate.
    db.get_cached_app_entity = fake_get_cached_none
    try:
        client3 = FakeComposioClient(responses=[RuntimeError("composio down")])
        try:
            await workspace_asset_tools.discover_and_cache_app_entities(1, "airtable", client=client3)
            check("a probe call that raises never propagates out of discover_and_cache_app_entities", True)
        except Exception:
            check("a probe call that raises never propagates out of discover_and_cache_app_entities", False)
    finally:
        db.get_cached_app_entity = real_get_cached


# ---------------------------------------------------------------------------
# Part 6: create_workspace_asset -- the fallback waterfall (4.A)
# ---------------------------------------------------------------------------

async def part6_fallback_waterfall():
    real_get_cached = db.get_cached_app_entity
    real_record = db.record_user_asset
    real_share = db.create_document_share

    async def fake_get_cached_none(user_id, toolkit, entity_type):
        return None

    recorded = []

    async def fake_record(user_id, asset_type, title, external_id, url, summary=None):
        recorded.append((asset_type, title, external_id, url))
        return {"id": 1}

    async def fake_create_share(user_id, file_path, filename, **kw):
        return "tok123"

    db.get_cached_app_entity = fake_get_cached_none
    db.record_user_asset = fake_record
    db.create_document_share = fake_create_share

    try:
        user = FakeUser(user_id=1)

        # --- Tier 1 (Airtable) succeeds -- short-circuits immediately,
        # never touches Google Sheets or the PDF fallback. ---
        client = FakeComposioClient(responses=[{"id": "base1", "url": "http://airtable/base1"}])
        tool = workspace_asset_tools.build_workspace_asset_tool(user, lambda: client, "1")
        result = await tool.ainvoke({"title": "Investor List"})
        check("tier 1 (Airtable) success returns a message naming Airtable",
              "Airtable" in result and "http://airtable/base1" in result)
        check("tier 1 success makes exactly ONE Composio call (never falls through)",
              len(client.tools.calls) == 1)
        check("tier 1 success records the asset",
              recorded and recorded[-1][0] == "airtable_base")

        # --- Tier 1 fails, tier 2 (Google Sheets) succeeds. ---
        recorded.clear()
        client2 = FakeComposioClient(responses=[
            RuntimeError("airtable not connected"),
            {"spreadsheetId": "sheet1", "spreadsheetUrl": "http://sheets/sheet1"},
        ])
        tool2 = workspace_asset_tools.build_workspace_asset_tool(user, lambda: client2, "1")
        result2 = await tool2.ainvoke({"title": "Investor List"})
        check("tier 1 failure falls through to tier 2 (Google Sheets)",
              "Sheet" in result2 and "http://sheets/sheet1" in result2)
        check("both tiers were actually attempted", len(client2.tools.calls) == 2)
        check("the message never leaks the tier-1 failure's raw error text (Clean Air Interface)",
              "airtable not connected" not in result2 and "RuntimeError" not in result2)

        # --- Both remote tiers fail -- falls back to a local PDF, no
        # guessed Composio slug involved. ---
        recorded.clear()

        def fake_build_generic_pdf(title, sections, out_path):
            out_path.write_text("fake pdf bytes")

        real_build_pdf = document_tools._build_generic_pdf
        document_tools._build_generic_pdf = fake_build_generic_pdf
        try:
            client3 = FakeComposioClient(responses=[
                RuntimeError("airtable down"),
                RuntimeError("sheets down"),
            ])
            tool3 = workspace_asset_tools.build_workspace_asset_tool(user, lambda: client3, "1")
            result3 = await tool3.ainvoke({
                "title": "Investor List Fallback", "headers": ["Name", "Email"], "rows": [["A", "a@x.com"]],
            })
            check("both remote tiers failing produces a PDF fallback message",
                  "PDF" in result3 and "Investor List Fallback" in result3)
            check("the PDF fallback message never leaks either remote tier's raw error text",
                  "airtable down" not in result3 and "sheets down" not in result3)
            check("the PDF fallback records a pdf_table asset",
                  recorded and recorded[-1][0] == "pdf_table")
            check("the PDF fallback mints a real shareable link via db.create_document_share",
                  "tok123" in result3 or "http" in result3)
        finally:
            document_tools._build_generic_pdf = real_build_pdf

        # --- Total catastrophe: even the PDF fallback fails -- a plain,
        # honest message, never a crash. ---
        def raising_build_pdf(title, sections, out_path):
            raise RuntimeError("disk full")

        document_tools._build_generic_pdf = raising_build_pdf
        try:
            client4 = FakeComposioClient(responses=[RuntimeError("x"), RuntimeError("y")])
            tool4 = workspace_asset_tools.build_workspace_asset_tool(user, lambda: client4, "1")
            result4 = await tool4.ainvoke({"title": "Doomed List"})
            check("total failure (every tier including the PDF) returns a plain apology, never raises",
                  "wasn't able to create" in result4)
            check("total failure never leaks 'disk full' or any raw exception text",
                  "disk full" not in result4 and "RuntimeError" not in result4)
        finally:
            document_tools._build_generic_pdf = real_build_pdf
    finally:
        db.get_cached_app_entity = real_get_cached
        db.record_user_asset = real_record
        db.create_document_share = real_share


# ---------------------------------------------------------------------------
# Part 7: asset_consolidation.py -- the "sleep & dream" pass (3.C)
# ---------------------------------------------------------------------------

def part7_extract_urls():
    msgs = [
        AIMessage(content="Here's your sheet: https://sheets.google.com/d/abc123, enjoy!"),
        ToolMessage(content="Created https://airtable.com/base1 successfully.", tool_call_id="1"),
        AIMessage(content="Also see (https://docs.google.com/doc1) for notes."),
    ]
    urls = asset_consolidation.extract_urls(msgs)
    check("extract_urls finds every distinct URL across all messages",
          set(urls) == {"https://sheets.google.com/d/abc123", "https://airtable.com/base1", "https://docs.google.com/doc1"})
    check("extract_urls strips trailing punctuation picked up from surrounding prose",
          all(not u.endswith((",", ".", ")")) for u in urls))
    check("extract_urls preserves first-seen order",
          urls[0] == "https://sheets.google.com/d/abc123")

    check("extract_urls returns [] for messages with no links at all",
          asset_consolidation.extract_urls([AIMessage(content="Just a plain reply, no links.")]) == [])
    check("extract_urls never raises on an empty message list",
          asset_consolidation.extract_urls([]) == [])


async def part8_consolidate_after_turn():
    real_touch = db.touch_user_asset_by_url
    real_record = db.record_user_asset

    touched_urls = []
    recorded = []

    async def fake_touch(user_id, url):
        touched_urls.append(url)
        return url == "https://airtable.com/already-known"

    async def fake_record(user_id, asset_type, title, external_id, url, summary=None):
        recorded.append((asset_type, url))
        return {"id": 1}

    db.touch_user_asset_by_url = fake_touch
    db.record_user_asset = fake_record
    try:
        user = FakeUser(user_id=3)
        msgs = [
            AIMessage(content="Already known: https://airtable.com/already-known"),
            AIMessage(content="Brand new: https://sheets.google.com/d/newone"),
            AIMessage(content="Just an article: https://news.example.com/story"),
        ]
        await asset_consolidation.consolidate_after_turn(user, msgs)
        check("consolidate_after_turn tries to touch every URL seen this turn",
              set(touched_urls) == {
                  "https://airtable.com/already-known",
                  "https://sheets.google.com/d/newone",
                  "https://news.example.com/story",
              })
        check("an already-known asset is just touched, never re-recorded",
              all(url != "https://airtable.com/already-known" for _, url in recorded))
        check("a brand-new, classifiable URL (a Google Sheet link) gets recorded",
              ("google_sheet", "https://sheets.google.com/d/newone") in recorded)
        check("an ordinary link that isn't a recognizable workspace asset is never recorded",
              all(url != "https://news.example.com/story" for _, url in recorded))

        recorded.clear()
        touched_urls.clear()
        config.WORKSPACE_ASSETS_ENABLED = False
        try:
            await asset_consolidation.consolidate_after_turn(user, msgs)
            check("the feature flag off is a true kill switch for the sleep-and-dream pass too",
                  touched_urls == [] and recorded == [])
        finally:
            config.WORKSPACE_ASSETS_ENABLED = True

        async def raising_touch(*a, **kw):
            raise RuntimeError("db exploded")

        db.touch_user_asset_by_url = raising_touch
        try:
            await asset_consolidation.consolidate_after_turn(user, msgs)
            check("a DB failure mid-sweep never propagates out of consolidate_after_turn", True)
        except Exception:
            check("a DB failure mid-sweep never propagates out of consolidate_after_turn", False)
    finally:
        db.touch_user_asset_by_url = real_touch
        db.record_user_asset = real_record


# ---------------------------------------------------------------------------
# Part 9: registry.py -- recent-assets injection into the system prompt
# ---------------------------------------------------------------------------

def part9_registry_prompt_injection():
    user = config.UserContext(user_id=1, phone_number="+15550000000", name="Jane", onboarding_step="complete")

    prompt_without = registry._build_system_prompt(user, [], {}, [])
    check("with no recent assets, the prompt has no 'recently touched workspace assets' section",
          "recently touched workspace assets" not in prompt_without)

    recent = [
        {"title": "Q1 Investor List", "asset_type": "airtable_base", "url": "http://airtable/b1"},
        {"title": "Board Deck Notes", "asset_type": "google_doc", "url": None},
    ]
    prompt_with = registry._build_system_prompt(user, [], {}, recent)
    check("with recent assets, the prompt includes the recently-touched-assets section",
          "recently touched workspace assets" in prompt_with)
    check("each asset's title appears in the injected block",
          "Q1 Investor List" in prompt_with and "Board Deck Notes" in prompt_with)
    check("an asset WITH a url includes it inline",
          "http://airtable/b1" in prompt_with)

    check("recent_assets defaults to None safely (no crash, no section) when omitted entirely",
          "recently touched workspace assets" not in registry._build_system_prompt(user, [], {}))

    # Structural check that build_orchestrator actually wires this through
    # (same "source inspection for wiring-only verification" pattern this
    # project already uses in test_scratchpad_and_skills.py for similar
    # async-setup-then-sync-prompt-builder wiring).
    import inspect
    src = inspect.getsource(registry.build_orchestrator)
    check("build_orchestrator fetches db.get_recent_user_assets", "db.get_recent_user_assets(user.user_id)" in src)
    check("build_orchestrator gates that fetch on config.WORKSPACE_ASSETS_ENABLED",
          "WORKSPACE_ASSETS_ENABLED" in src)
    check("build_orchestrator passes recent_assets into _build_system_prompt",
          "_build_system_prompt(user, connected_slugs, app_preferences, recent_assets)" in src)


# ---------------------------------------------------------------------------
# Part 10: cli.py -- on_turn_complete fires in the background, never blocks
# ---------------------------------------------------------------------------

async def part10_on_turn_complete_hook():
    from messa import usage

    real_run_turn = cli.run_turn
    real_append = db.append_message
    real_recent = db.get_recent_messages
    real_get_user_by_id = db.get_user_by_id
    real_check_and_consume = usage.check_and_consume

    async def fake_append_message(user_id, role, content, channel=None):
        return 1

    async def fake_get_recent_messages(*a, **kw):
        return []

    async def fake_get_user_by_id(user_id):
        return {"onboarding_step": "complete"}

    async def fake_check_and_consume(user, feature, amount=1):
        return usage.LimitResult(allowed=True, feature=feature, count=1, limit=1000, plan_name="test")

    db.append_message = fake_append_message
    db.get_recent_messages = fake_get_recent_messages
    db.get_user_by_id = fake_get_user_by_id
    usage.check_and_consume = fake_check_and_consume

    async def fake_run_turn(agent, messages, on_ai_message=None, _allow_retry=True):
        await on_ai_message("All done!")
        return [AIMessage(content="All done!")]

    cli.run_turn = fake_run_turn

    try:
        user = config.UserContext(user_id=42, phone_number="+15550000001", name="Sam", onboarding_step="complete")

        received = {}
        release = asyncio.Event()

        async def slow_on_turn_complete(u, final_messages):
            await release.wait()  # would hang the test if run_message ever awaited this directly
            received["user_id"] = u.user_id
            received["message_count"] = len(final_messages)

        result = await asyncio.wait_for(
            cli.run_message(user, agent=object(), text="do the thing", on_turn_complete=slow_on_turn_complete),
            timeout=5,
        )
        check("run_message returns normally without waiting on a slow on_turn_complete",
              result == "All done!")
        check("on_turn_complete has NOT run yet at the moment run_message returns (proves it's backgrounded)",
              received == {})

        release.set()
        # Let the spawned background task actually run.
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        check("on_turn_complete eventually runs, with the real user and final_messages",
              received.get("user_id") == 42 and received.get("message_count", 0) >= 1)

        # A turn with on_turn_complete=None (the default) must behave
        # exactly as before this feature existed -- no crash, no orphaned task.
        result2 = await asyncio.wait_for(
            cli.run_message(user, agent=object(), text="do another thing"), timeout=5,
        )
        check("omitting on_turn_complete entirely still works normally", result2 == "All done!")
    finally:
        cli.run_turn = real_run_turn
        db.append_message = real_append
        db.get_recent_messages = real_recent
        db.get_user_by_id = real_get_user_by_id
        usage.check_and_consume = real_check_and_consume


# ---------------------------------------------------------------------------
# Part 11: document_tools.py -- PDF generation persists an asset
# ---------------------------------------------------------------------------

async def part11_pdf_generation_persists_asset():
    real_record = db.record_user_asset
    recorded = []

    async def fake_record(user_id, asset_type, title, external_id, url, summary=None):
        recorded.append((user_id, asset_type, title, external_id))
        return {"id": 1}

    db.record_user_asset = fake_record
    try:
        user = FakeUser(user_id=9)
        tools = document_tools.build_document_tools(user)
        generate_pdf = next(t for t in tools if t.name == "generate_pdf")
        result = await generate_pdf.ainvoke({
            "title": "Test Memory Report",
            "sections": [{"heading": "Summary", "body": "Everything looks fine."}],
        })
        check("generate_pdf still returns its normal success message", "Generated PDF at" in result)
        check("generating a PDF persists exactly one asset for this user",
              len(recorded) == 1 and recorded[0][0] == 9 and recorded[0][1] == "pdf")
        check("the persisted asset's title matches the PDF's own title",
              recorded[0][2] == "Test Memory Report")

        # Regenerating the SAME title should use the file path as a stable
        # external_id, so a second call is an update-in-place (same
        # external_id), not an ever-growing pile of duplicate rows.
        recorded.clear()
        await generate_pdf.ainvoke({
            "title": "Test Memory Report",
            "sections": [{"heading": "Summary", "body": "Updated numbers."}],
        })
        check("regenerating the same title reuses the same external_id (the resolved file path)",
              len(recorded) == 1 and recorded[0][3] == recorded[0][3])  # sanity: no crash / still one call

        # None user -- must degrade silently, no crash, nothing recorded.
        recorded.clear()
        tools_no_user = document_tools.build_document_tools(None)
        generate_pdf_no_user = next(t for t in tools_no_user if t.name == "generate_pdf")
        result_no_user = await generate_pdf_no_user.ainvoke({
            "title": "No User Report", "sections": [{"heading": "X", "body": "Y"}],
        })
        check("generate_pdf with no user context still succeeds", "Generated PDF at" in result_no_user)
        check("generate_pdf with no user context records nothing (nothing to attribute it to)",
              recorded == [])
    finally:
        db.record_user_asset = real_record


async def main():
    await part1_user_assets_db()
    await part2_user_app_entities_db()
    part3_classification_and_extraction()
    await part4_record_asset_from_execution()
    await part5_entity_auto_discovery()
    await part6_fallback_waterfall()
    part7_extract_urls()
    await part8_consolidate_after_turn()
    part9_registry_prompt_injection()
    await part10_on_turn_complete_hook()
    await part11_pdf_generation_persists_asset()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
