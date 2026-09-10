"""Tests for V3-autonomous.md Phase 5 ("Meeting Dossiers & Commitment
Ledger"), built on top of Phases 1-4 (already merged to main):

  1. messa/commitments.py's pure helpers (_parse_model_json, _parse_due_date,
     _guess_counterparty_name) and extract_commitment against a fake model
     (happy path, "no commitment" reply, malformed JSON, and a raised
     exception -- all degrade to None, never raise).
  2. commitments.maybe_record_commitment: the MEETING_DOSSIERS_ENABLED kill
     switch, the empty-body/empty-address no-op, and the actual
     fire-and-forget round trip into db.insert_commitment.
  3. db.py's new user_commitments functions (insert_commitment,
     list_open_commitments_for_hints, list_commitments_due_for_nudge,
     mark_commitment_nudge_sent) against a fake asyncpg pool/connection,
     plus the pre-migration graceful degrade.
  4. db.py's new meeting_dossier_events / pending_post_meeting_notes
     functions (get_due_native_pre_meeting_events,
     get_due_native_post_meeting_events, list_users_with_connected_
     calendar_primary, mark_pre_brief_sent, mark_post_harvest_sent,
     is_dossier_event_already_sent, set_pending_post_meeting_note,
     pop_pending_post_meeting_note) against the same fake pool.
  5. messa/meeting_dossiers.py's pure template helpers (_normalize_native_
     row, _title_hints, _event_hints, _clock, _render_pre_meeting_brief,
     _render_post_meeting_prompt).
  6. messa/meeting_dossiers.py's orchestration (get_due_pre_meeting_briefs/
     get_due_post_meeting_harvests): native + connected sources merged,
     already-sent connected events skipped, and the
     MEETING_DOSSIERS_ENABLED kill switch.
  7. messa/tools/integration_tools.py's connected-calendar read path:
     _normalize_connected_event (timed event, all-day event skipped,
     missing id skipped), _discover_calendar_list_events_slug (cache hit/
     miss, LIST+EVENT preference, fallback to top result, exception ->
     None cached), and list_connected_calendar_events end-to-end against a
     fake Composio client.
  8. messa/agents/registry.py's _pending_meeting_note_paragraph: no note ->
     "", a note with/without a counterparty name.
  9. The send_email/reply_to_email hook points in tools/email_tools.py and
     tools/personal_inbox_tools.py: commitments.maybe_record_commitment is
     called after a successful send/reply, and NOT called after a failed
     one.

No live Postgres, no live LLM call, no live Composio account.
"""
import asyncio
import json
import os
import sys

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")
    os.environ.setdefault("SENDBLUE_API_KEY", "sb-test-dummy-key")
    os.environ.setdefault("SENDBLUE_API_SECRET", "sb-test-dummy-secret")

from messa import commitments, config, db, meeting_dossiers, usage  # noqa: E402
from messa.agents import registry  # noqa: E402
from messa.tools import email_tools, integration_tools, personal_inbox_tools  # noqa: E402
from messa.channels.resend import ResendError  # noqa: E402
from messa.approval import AutoApproveGate  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _user(**kwargs) -> config.UserContext:
    base = dict(user_id=1, phone_number="+15550000000", name="Jane", timezone="America/New_York")
    base.update(kwargs)
    return config.UserContext(**base)


# ---------------------------------------------------------------------------
# Fakes -- same FakeAcquire/FakePool/FakeConn/FakeRow shape as
# test_v3_phase3_email_triage.py / test_v3_phase4_latency.py.
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
        return None

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


class FakeAIMessage:
    def __init__(self, content):
        self.content = content


class FakeModel:
    def __init__(self, content=None, exc=None):
        self.content = content
        self.exc = exc
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.exc:
            raise self.exc
        return FakeAIMessage(self.content)


# ---------------------------------------------------------------------------
# Part 1: commitments.py pure helpers + extract_commitment
# ---------------------------------------------------------------------------

async def part1_extract_commitment():
    check("_parse_model_json: plain JSON object", commitments._parse_model_json('{"a": 1}') == {"a": 1})
    check("_parse_model_json: fenced ```json``` block", commitments._parse_model_json('```json\n{"a": 1}\n```') == {"a": 1})
    check("_parse_model_json: empty object -> None (no commitment)", commitments._parse_model_json("{}") is None)
    check("_parse_model_json: garbage text -> None", commitments._parse_model_json("not json at all") is None)
    check("_parse_model_json: empty string -> None", commitments._parse_model_json("") is None)

    check("_parse_due_date: valid ISO date", commitments._parse_due_date("2026-09-17") == date(2026, 9, 17))
    check("_parse_due_date: garbage -> None", commitments._parse_due_date("Thursday") is None)
    check("_parse_due_date: None -> None", commitments._parse_due_date(None) is None)

    name = await commitments._guess_counterparty_name("sarah.chen@acme.com")
    check("_guess_counterparty_name: dotted local part -> title-cased name", name == "Sarah Chen")
    check("_guess_counterparty_name: numeric local part -> None",
          await commitments._guess_counterparty_name("12345@acme.com") is None)

    real_build_model = config.build_model
    try:
        # Happy path: a real commitment.
        config.build_model = lambda *a, **k: FakeModel(
            content='{"commitment_summary": "send the updated deck", "due_date": "2026-09-17", '
                    '"excerpt": "I will send the updated deck by Thursday"}'
        )
        result = await commitments.extract_commitment("I'll send the updated deck by Thursday, talk soon!")
        check("extract_commitment: happy path returns a dict", result is not None)
        check("extract_commitment: happy path summary", result and result["commitment_summary"] == "send the updated deck")
        check("extract_commitment: happy path due_date parsed", result and result["due_date"] == date(2026, 9, 17))

        # No real commitment -> model replies {}.
        config.build_model = lambda *a, **k: FakeModel(content="{}")
        result = await commitments.extract_commitment("Just checking in, no rush on anything.")
        check("extract_commitment: model's own {} (no commitment) -> None", result is None)

        # Malformed reply -> None, never raises.
        config.build_model = lambda *a, **k: FakeModel(content="I'm not sure how to answer that")
        result = await commitments.extract_commitment("hello")
        check("extract_commitment: malformed JSON reply -> None", result is None)

        # The model call itself raises -> None, never propagates.
        config.build_model = lambda *a, **k: FakeModel(exc=RuntimeError("boom"))
        result = await commitments.extract_commitment("I'll get you the numbers tomorrow")
        check("extract_commitment: model call raising -> None (never propagates)", result is None)

        # Empty body short-circuits before any model call.
        config.build_model = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called"))
        result = await commitments.extract_commitment("   ")
        check("extract_commitment: blank body never calls the model", result is None)
    finally:
        config.build_model = real_build_model


# ---------------------------------------------------------------------------
# Part 2: commitments.maybe_record_commitment -- kill switch, empty-input
# no-op, and the actual fire-and-forget round trip.
# ---------------------------------------------------------------------------

async def part2_maybe_record_commitment():
    real_enabled = config.MEETING_DOSSIERS_ENABLED
    real_extract = commitments.extract_commitment
    real_insert = db.insert_commitment
    try:
        calls = []

        async def fake_extract(body, **kwargs):
            calls.append(("extract", body))
            return {"commitment_summary": "send the deck", "due_date": date(2026, 9, 17), "excerpt": "..."}

        async def fake_insert(user_id, summary, **kwargs):
            calls.append(("insert", user_id, summary, kwargs))
            return {"id": 1}

        commitments.extract_commitment = fake_extract
        db.insert_commitment = fake_insert

        config.MEETING_DOSSIERS_ENABLED = False
        commitments.maybe_record_commitment(1, "to@example.com", "I'll send it by Friday")
        await asyncio.sleep(0)
        check("maybe_record_commitment: MEETING_DOSSIERS_ENABLED=False is a no-op", calls == [])

        config.MEETING_DOSSIERS_ENABLED = True
        commitments.maybe_record_commitment(1, "", "I'll send it by Friday")
        commitments.maybe_record_commitment(1, "to@example.com", "   ")
        await asyncio.sleep(0)
        check("maybe_record_commitment: empty to_address/body is a no-op", calls == [])

        commitments.maybe_record_commitment(7, "boss@example.com", "I'll send the deck by Friday")
        # Let the detached task actually run.
        for _ in range(5):
            await asyncio.sleep(0)
        check("maybe_record_commitment: fires the extraction call", ("extract", "I'll send the deck by Friday") in calls)
        insert_calls = [c for c in calls if c[0] == "insert"]
        check("maybe_record_commitment: records the found commitment", len(insert_calls) == 1)
        check("maybe_record_commitment: records under the right user_id", insert_calls and insert_calls[0][1] == 7)
        check(
            "maybe_record_commitment: passes the counterparty email through",
            insert_calls and insert_calls[0][3].get("counterparty_email") == "boss@example.com",
        )
    finally:
        config.MEETING_DOSSIERS_ENABLED = real_enabled
        commitments.extract_commitment = real_extract
        db.insert_commitment = real_insert


# ---------------------------------------------------------------------------
# Part 3: db.py user_commitments functions
# ---------------------------------------------------------------------------

async def part3_db_commitments():
    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    check("insert_commitment: pre-migration -> None", await db.insert_commitment(1, "do the thing") is None)
    check("list_open_commitments_for_hints: pre-migration -> []",
          await db.list_open_commitments_for_hints(1, ["Sarah"]) == [])
    check("list_commitments_due_for_nudge: pre-migration -> []", await db.list_commitments_due_for_nudge() == [])
    # mark_commitment_nudge_sent is a silent no-op pre-migration -- just
    # confirm it doesn't raise.
    await db.mark_commitment_nudge_sent(1)
    check("mark_commitment_nudge_sent: pre-migration doesn't raise", True)

    check("list_open_commitments_for_hints: no hints at all -> [] without a DB call",
          await db.list_open_commitments_for_hints(1) == [])

    conn = FakeConn(has_tables=True, fetchrow_queue=[FakeRow(id=1, user_id=7, commitment_summary="send the deck")])
    install_fake_pool(conn)
    row = await db.insert_commitment(
        7, "send the deck", source_excerpt="I'll send the deck", due_date=date(2026, 9, 17),
        counterparty_name="Sarah Chen", counterparty_email="sarah@acme.com",
    )
    check("insert_commitment: returns the inserted row", row is not None and row["id"] == 1)
    insert_call = conn.calls[-1]
    check("insert_commitment: real INSERT issued", insert_call[0] == "fetchrow" and "INSERT INTO user_commitments" in insert_call[1])

    conn = FakeConn(has_tables=True, fetch_queue=[[FakeRow(id=2, commitment_summary="send the deck")]])
    install_fake_pool(conn)
    hits = await db.list_open_commitments_for_hints(7, ["Sarah", "Chen"], ["sarah@acme.com"])
    check("list_open_commitments_for_hints: returns matches", hits and hits[0]["id"] == 2)

    conn = FakeConn(has_tables=True, fetch_queue=[[FakeRow(id=3, phone_number="+15551234567", commitment_summary="x")]])
    install_fake_pool(conn)
    due = await db.list_commitments_due_for_nudge()
    check("list_commitments_due_for_nudge: returns due rows joined with phone_number",
          due and due[0]["phone_number"] == "+15551234567")

    conn = FakeConn(has_tables=True)
    install_fake_pool(conn)
    await db.mark_commitment_nudge_sent(3)
    check("mark_commitment_nudge_sent: real UPDATE issued",
          conn.calls[-1][0] == "execute" and "user_commitments" in conn.calls[-1][1])


# ---------------------------------------------------------------------------
# Part 4: db.py meeting_dossier_events / pending_post_meeting_notes
# ---------------------------------------------------------------------------

async def part4_db_dossier_events():
    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    check("get_due_native_pre_meeting_events: pre-migration -> []",
          await db.get_due_native_pre_meeting_events() == [])
    check("get_due_native_post_meeting_events: pre-migration -> []",
          await db.get_due_native_post_meeting_events() == [])
    check("is_dossier_event_already_sent: pre-migration -> False",
          await db.is_dossier_event_already_sent(1, "native:1", "pre_brief") is False)
    check("pop_pending_post_meeting_note: pre-migration -> None",
          await db.pop_pending_post_meeting_note(1) is None)
    await db.mark_pre_brief_sent(1, "native:1")
    await db.mark_post_harvest_sent(1, "native:1")
    await db.set_pending_post_meeting_note(1, "Coffee with Sarah", "Sarah")
    check("mark_pre_brief_sent/mark_post_harvest_sent/set_pending_post_meeting_note: pre-migration doesn't raise", True)

    conn = FakeConn(has_tables=True, fetch_queue=[[FakeRow(id=10, user_id=1, title="Coffee with Sarah", phone_number="+1555")]])
    install_fake_pool(conn)
    due = await db.get_due_native_pre_meeting_events()
    check("get_due_native_pre_meeting_events: returns due native rows", due and due[0]["id"] == 10)

    conn = FakeConn(has_tables=True, fetch_queue=[[FakeRow(user_id=2, phone_number="+1555", preferred_app="googlecalendar")]])
    install_fake_pool(conn)
    users = await db.list_users_with_connected_calendar_primary()
    check("list_users_with_connected_calendar_primary: returns connected-calendar users",
          users and users[0]["preferred_app"] == "googlecalendar")

    conn = FakeConn(has_tables=True, fetchval_queue=[None])
    install_fake_pool(conn)
    check("is_dossier_event_already_sent: no row yet -> False",
          await db.is_dossier_event_already_sent(1, "googlecalendar:abc", "pre_brief") is False)

    conn = FakeConn(has_tables=True, fetchval_queue=[datetime.now(timezone.utc)])
    install_fake_pool(conn)
    check("is_dossier_event_already_sent: sent_at present -> True",
          await db.is_dossier_event_already_sent(1, "googlecalendar:abc", "post_harvest") is True)

    conn = FakeConn(has_tables=True, fetchrow_queue=[FakeRow(user_id=1, event_title="Coffee", counterparty_name="Sarah")])
    install_fake_pool(conn)
    note = await db.pop_pending_post_meeting_note(1)
    check("pop_pending_post_meeting_note: returns the row when present", note is not None and note["event_title"] == "Coffee")

    conn = FakeConn(has_tables=True, fetchrow_queue=[None])
    install_fake_pool(conn)
    note = await db.pop_pending_post_meeting_note(1)
    check("pop_pending_post_meeting_note: returns None (and tries the expired-cleanup delete) when missing/expired",
          note is None and any(c[0] == "execute" for c in conn.calls))


# ---------------------------------------------------------------------------
# Part 5: meeting_dossiers.py pure template helpers
# ---------------------------------------------------------------------------

async def part5_meeting_dossiers_templates():
    row = {
        "id": 5, "user_id": 1, "title": "Coffee with Sarah Chen",
        "start_time": datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc),
        "end_time": datetime(2026, 9, 17, 14, 30, tzinfo=timezone.utc),
        "location": "Blue Bottle", "notes": "Discuss Q3 roadmap",
    }
    event = meeting_dossiers._normalize_native_row(row)
    check("_normalize_native_row: event_key", event["event_key"] == "native:5")
    check("_normalize_native_row: title carried through", event["title"] == "Coffee with Sarah Chen")
    check("_normalize_native_row: attendees always empty for native events", event["attendees"] == [])

    hints = meeting_dossiers._title_hints("Coffee with Sarah Chen")
    check("_title_hints: drops stopwords, keeps real name tokens", hints == ["Coffee", "Sarah", "Chen"])
    check("_title_hints: drops short/common words entirely",
          "with" not in meeting_dossiers._title_hints("Sync with Bob"))

    names, emails = meeting_dossiers._event_hints(event)
    check("_event_hints: native event -> title-derived name hints, no emails", "Sarah" in names and emails == [])

    connected_event = dict(event, attendees=[{"email": "sarah@acme.com", "name": "Sarah Chen"}])
    names2, emails2 = meeting_dossiers._event_hints(connected_event)
    check("_event_hints: connected event -> attendee email/name hints included",
          "sarah@acme.com" in emails2 and "Sarah Chen" in names2)

    tz = __import__("zoneinfo").ZoneInfo("America/New_York")
    clock_str = meeting_dossiers._clock(datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc), tz)
    check("_clock: renders a no-leading-zero 12-hour time", clock_str and clock_str[0] != "0")

    real_hints_fn = db.list_open_commitments_for_hints
    try:
        db.list_open_commitments_for_hints = lambda *a, **k: asyncio.sleep(0, result=[])
        text = await meeting_dossiers._render_pre_meeting_brief(1, event, "America/New_York")
        check("_render_pre_meeting_brief: includes the event title", "Coffee with Sarah Chen" in text)
        check("_render_pre_meeting_brief: includes notes when present", "Discuss Q3 roadmap" in text)
        check("_render_pre_meeting_brief: no 'Open:' line when there are no open commitments", "Open:" not in text)

        db.list_open_commitments_for_hints = lambda *a, **k: asyncio.sleep(
            0, result=[{"commitment_summary": "send the updated deck"}]
        )
        text2 = await meeting_dossiers._render_pre_meeting_brief(1, event, "America/New_York")
        check("_render_pre_meeting_brief: surfaces an open commitment", "send the updated deck" in text2)
    finally:
        db.list_open_commitments_for_hints = real_hints_fn

    prompt = meeting_dossiers._render_post_meeting_prompt(event)
    check("_render_post_meeting_prompt: names the event and asks for a voice note",
          "Coffee with Sarah Chen" in prompt and "voice note" in prompt)


# ---------------------------------------------------------------------------
# Part 6: meeting_dossiers.py orchestration
# ---------------------------------------------------------------------------

async def part6_meeting_dossiers_orchestration():
    real_enabled = config.MEETING_DOSSIERS_ENABLED
    real_native_pre = db.get_due_native_pre_meeting_events
    real_native_post = db.get_due_native_post_meeting_events
    real_conn_users = db.list_users_with_connected_calendar_primary
    real_already_sent = db.is_dossier_event_already_sent
    real_list_connected = integration_tools.list_connected_calendar_events
    real_hints_fn = db.list_open_commitments_for_hints
    try:
        config.MEETING_DOSSIERS_ENABLED = False
        check("get_due_pre_meeting_briefs: kill switch -> []", await meeting_dossiers.get_due_pre_meeting_briefs() == [])
        check("get_due_post_meeting_harvests: kill switch -> []", await meeting_dossiers.get_due_post_meeting_harvests() == [])

        config.MEETING_DOSSIERS_ENABLED = True
        now = datetime(2026, 9, 17, 13, 50, tzinfo=timezone.utc)

        native_row = {
            "id": 1, "user_id": 1, "phone_number": "+15551111111", "user_timezone": "America/New_York",
            "title": "Native Meeting", "start_time": now + timedelta(minutes=8),
            "end_time": now + timedelta(minutes=38), "location": None, "notes": None,
        }
        connected_user_row = {"user_id": 2, "phone_number": "+15552222222", "user_timezone": "UTC", "preferred_app": "googlecalendar"}
        connected_event_due = {
            "event_key": "googlecalendar:abc", "title": "Connected Meeting",
            "start_time": now + timedelta(minutes=9), "end_time": now + timedelta(minutes=39),
            "location": None, "notes": None, "attendees": [],
        }
        connected_event_already_sent = {
            "event_key": "googlecalendar:zzz", "title": "Already Sent",
            "start_time": now + timedelta(minutes=9), "end_time": now + timedelta(minutes=39),
            "location": None, "notes": None, "attendees": [],
        }

        db.get_due_native_pre_meeting_events = lambda *a, **k: asyncio.sleep(0, result=[native_row])
        db.list_users_with_connected_calendar_primary = lambda: asyncio.sleep(0, result=[connected_user_row])
        integration_tools.list_connected_calendar_events = lambda *a, **k: asyncio.sleep(
            0, result=[connected_event_due, connected_event_already_sent]
        )

        async def fake_already_sent(user_id, event_key, kind):
            return event_key == "googlecalendar:zzz"

        db.is_dossier_event_already_sent = fake_already_sent
        db.list_open_commitments_for_hints = lambda *a, **k: asyncio.sleep(0, result=[])

        briefs = await meeting_dossiers.get_due_pre_meeting_briefs(now)
        check("get_due_pre_meeting_briefs: includes the native event", any(b["event_key"] == "native:1" for b in briefs))
        check("get_due_pre_meeting_briefs: includes the due connected event",
              any(b["event_key"] == "googlecalendar:abc" for b in briefs))
        check("get_due_pre_meeting_briefs: skips the already-sent connected event",
              not any(b["event_key"] == "googlecalendar:zzz" for b in briefs))
        check("get_due_pre_meeting_briefs: exactly 2 items", len(briefs) == 2)

        native_ended_row = {
            "id": 2, "user_id": 1, "phone_number": "+15551111111", "user_timezone": "America/New_York",
            "title": "Wrapped-up Meeting", "start_time": now - timedelta(minutes=35),
            "end_time": now - timedelta(minutes=2), "location": None, "notes": None,
        }
        db.get_due_native_post_meeting_events = lambda *a, **k: asyncio.sleep(0, result=[native_ended_row])
        harvests = await meeting_dossiers.get_due_post_meeting_harvests(now)
        check("get_due_post_meeting_harvests: includes the native ended event",
              any(h["event_key"] == "native:2" for h in harvests))
    finally:
        config.MEETING_DOSSIERS_ENABLED = real_enabled
        db.get_due_native_pre_meeting_events = real_native_pre
        db.get_due_native_post_meeting_events = real_native_post
        db.list_users_with_connected_calendar_primary = real_conn_users
        db.is_dossier_event_already_sent = real_already_sent
        integration_tools.list_connected_calendar_events = real_list_connected
        db.list_open_commitments_for_hints = real_hints_fn


# ---------------------------------------------------------------------------
# Part 7: integration_tools.py connected-calendar read path
# ---------------------------------------------------------------------------

class FakeToolsClient:
    def __init__(self, search_results=None, execute_result=None, execute_exc=None):
        self.search_results = search_results if search_results is not None else []
        self.execute_result = execute_result
        self.execute_exc = execute_exc
        self.execute_calls = []
        self.get_calls = []

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return self.search_results

    def execute(self, **kwargs):
        self.execute_calls.append(kwargs)
        if self.execute_exc:
            raise self.execute_exc
        return self.execute_result


class FakeComposioClient:
    def __init__(self, tools_client):
        self.tools = tools_client


async def part7_integration_tools_connected_calendar():
    # _normalize_connected_event
    timed = {
        "id": "evt1", "summary": "Coffee with Sarah",
        "start": {"dateTime": "2026-09-17T14:00:00Z"}, "end": {"dateTime": "2026-09-17T14:30:00Z"},
        "location": "Cafe", "description": "notes here",
        "attendees": [{"email": "sarah@acme.com", "displayName": "Sarah Chen"}],
    }
    event = integration_tools._normalize_connected_event(timed)
    check("_normalize_connected_event: parses a timed event", event is not None and event["event_key"] == "googlecalendar:evt1")
    check("_normalize_connected_event: parses start_time as tz-aware", event["start_time"].tzinfo is not None)
    check("_normalize_connected_event: carries attendees through", event["attendees"] == [{"email": "sarah@acme.com", "name": "Sarah Chen"}])

    all_day = {"id": "evt2", "summary": "Someone's birthday", "start": {"date": "2026-09-17"}, "end": {"date": "2026-09-18"}}
    check("_normalize_connected_event: an all-day event (no dateTime) is skipped",
          integration_tools._normalize_connected_event(all_day) is None)

    no_id = {"summary": "No id", "start": {"dateTime": "2026-09-17T14:00:00Z"}}
    check("_normalize_connected_event: missing id is skipped", integration_tools._normalize_connected_event(no_id) is None)

    # _discover_calendar_list_events_slug
    integration_tools._calendar_list_slug_cache.clear()
    client = FakeComposioClient(FakeToolsClient(search_results=[
        {"slug": "GOOGLECALENDAR_FIND_EVENT", "description": "find one event"},
        {"slug": "GOOGLECALENDAR_LIST_EVENTS", "description": "list calendar events"},
    ]))
    slug = integration_tools._discover_calendar_list_events_slug(client, "u1", "googlecalendar")
    check("_discover_calendar_list_events_slug: prefers a LIST+EVENT-named result", slug == "GOOGLECALENDAR_LIST_EVENTS")
    check("_discover_calendar_list_events_slug: caches the result",
          integration_tools._calendar_list_slug_cache.get("googlecalendar") == "GOOGLECALENDAR_LIST_EVENTS")

    integration_tools._calendar_list_slug_cache.clear()
    client2 = FakeComposioClient(FakeToolsClient(search_results=[{"slug": "GOOGLECALENDAR_SOMETHING_ELSE", "description": "x"}]))
    slug2 = integration_tools._discover_calendar_list_events_slug(client2, "u1", "googlecalendar")
    check("_discover_calendar_list_events_slug: falls back to the top result when nothing matches LIST+EVENT",
          slug2 == "GOOGLECALENDAR_SOMETHING_ELSE")

    integration_tools._calendar_list_slug_cache.clear()
    client3 = FakeComposioClient(FakeToolsClient())
    client3.tools.get = lambda **k: (_ for _ in ()).throw(RuntimeError("composio down"))
    slug3 = integration_tools._discover_calendar_list_events_slug(client3, "u1", "googlecalendar")
    check("_discover_calendar_list_events_slug: a search failure returns/caches None instead of raising", slug3 is None)
    check("_discover_calendar_list_events_slug: None is cached too (no repeat search)",
          "googlecalendar" in integration_tools._calendar_list_slug_cache)

    # list_connected_calendar_events end-to-end
    real_get_client = integration_tools._get_client
    try:
        integration_tools._calendar_list_slug_cache.clear()
        fake_events = [
            {"id": "evt1", "summary": "In window", "start": {"dateTime": "2026-09-17T14:05:00Z"}, "end": {"dateTime": "2026-09-17T14:30:00Z"}},
            {"id": "evt2", "summary": "Out of window", "start": {"dateTime": "2026-09-18T14:05:00Z"}, "end": {"dateTime": "2026-09-18T14:30:00Z"}},
        ]
        tools_client = FakeToolsClient(
            search_results=[{"slug": "GOOGLECALENDAR_LIST_EVENTS", "description": "list events"}],
            execute_result={"data": {"items": fake_events}},
        )
        integration_tools._get_client = lambda: FakeComposioClient(tools_client)
        user = _user(user_id=9)
        start = datetime(2026, 9, 17, 14, 0, tzinfo=timezone.utc)
        end = datetime(2026, 9, 17, 14, 20, tzinfo=timezone.utc)
        events = await integration_tools.list_connected_calendar_events(user, "googlecalendar", start, end)
        check("list_connected_calendar_events: returns only the in-window event", len(events) == 1 and events[0]["event_key"] == "googlecalendar:evt1")
        check("list_connected_calendar_events: skips non-googlecalendar toolkits",
              await integration_tools.list_connected_calendar_events(user, "outlookcalendar", start, end) == [])

        # Execute failure degrades to [] rather than raising.
        tools_client2 = FakeToolsClient(
            search_results=[{"slug": "GOOGLECALENDAR_LIST_EVENTS", "description": "list events"}],
            execute_exc=RuntimeError("api down"),
        )
        integration_tools._calendar_list_slug_cache.clear()
        integration_tools._get_client = lambda: FakeComposioClient(tools_client2)
        events2 = await integration_tools.list_connected_calendar_events(user, "googlecalendar", start, end)
        check("list_connected_calendar_events: an execute failure degrades to []", events2 == [])
    finally:
        integration_tools._get_client = real_get_client
        integration_tools._calendar_list_slug_cache.clear()


# ---------------------------------------------------------------------------
# Part 8: registry._pending_meeting_note_paragraph
# ---------------------------------------------------------------------------

async def part8_pending_meeting_note_paragraph():
    check("_pending_meeting_note_paragraph: no note -> ''", registry._pending_meeting_note_paragraph(None) == "")
    check("_pending_meeting_note_paragraph: empty dict -> ''", registry._pending_meeting_note_paragraph({}) == "")

    text = registry._pending_meeting_note_paragraph({"event_title": "Coffee with Sarah", "counterparty_name": "Sarah"})
    check("_pending_meeting_note_paragraph: mentions the event title", "Coffee with Sarah" in text)
    check("_pending_meeting_note_paragraph: mentions the counterparty", "with Sarah" in text)

    text2 = registry._pending_meeting_note_paragraph({"event_title": "Standup", "counterparty_name": None})
    check("_pending_meeting_note_paragraph: no counterparty -> no dangling 'with'", "with Standup" not in text2 and "Standup" in text2)


# ---------------------------------------------------------------------------
# Part 9: send_email/reply_to_email hook points
# ---------------------------------------------------------------------------

class FakeGmailToolsClient:
    def __init__(self, result):
        self.result = result

    def execute(self, **kwargs):
        return self.result


async def part9_send_hooks():
    real_maybe_record = commitments.maybe_record_commitment
    real_get_client = email_tools._get_client
    real_resend_send = personal_inbox_tools.resend_send_email
    real_get_thread = personal_inbox_tools.db.get_latest_inbound_message_in_thread
    real_check_and_consume = usage.check_and_consume
    calls = []

    def fake_maybe_record(user_id, to_address, body, **kwargs):
        calls.append((user_id, to_address, body))

    async def fake_check_and_consume(user, feature, amount=1):
        return usage.LimitResult(allowed=True, feature=feature, count=0, limit=None, plan_name="test")

    commitments.maybe_record_commitment = fake_maybe_record
    usage.check_and_consume = fake_check_and_consume
    try:
        # email_tools.py (Gmail) send_email -- success calls the hook.
        user = _user(user_id=3, email_connected=True)
        email_tools._get_client = lambda: type("C", (), {"tools": FakeGmailToolsClient({"successful": True})})()
        tools = {t.name: t for t in email_tools.build_email_tools(user, approval_gate=AutoApproveGate())}
        result = await tools["send_email"].coroutine(to="client@example.com", subject="Hi", body="I'll send it by Friday")
        check("email_tools.send_email: successful send fires the commitment hook",
              (3, "client@example.com", "I'll send it by Friday") in calls)

        calls.clear()
        email_tools._get_client = lambda: type("C", (), {"tools": FakeGmailToolsClient({"successful": False, "error": "nope"})})()
        await tools["send_email"].coroutine(to="client@example.com", subject="Hi", body="I'll send it by Friday")
        check("email_tools.send_email: a failed send does NOT fire the commitment hook", calls == [])

        # personal_inbox_tools.py send_email -- success calls the hook.
        calls.clear()
        user2 = _user(user_id=4, messa_email_local_part="jane")
        config_real_key = config.RESEND_API_KEY
        config.RESEND_API_KEY = "re_test_dummy"

        async def fake_resend_ok(*a, **k):
            return None

        personal_inbox_tools.resend_send_email = fake_resend_ok
        tools2 = {t.name: t for t in personal_inbox_tools.build_personal_inbox_tools(user2, approval_gate=AutoApproveGate())}
        await tools2["send_email"].coroutine(to="someone@example.com", subject="Hi", body="I'll get you the report Monday")
        check("personal_inbox_tools.send_email: successful send fires the commitment hook",
              (4, "someone@example.com", "I'll get you the report Monday") in calls)

        calls.clear()

        async def fake_resend_fail(*a, **k):
            raise ResendError("send failed")

        personal_inbox_tools.resend_send_email = fake_resend_fail
        await tools2["send_email"].coroutine(to="someone@example.com", subject="Hi", body="I'll get you the report Monday")
        check("personal_inbox_tools.send_email: a failed send does NOT fire the commitment hook", calls == [])

        # personal_inbox_tools.py reply_to_email -- success calls the hook
        # with the resolved from_address.
        calls.clear()
        personal_inbox_tools.resend_send_email = fake_resend_ok

        async def fake_get_thread(user_id, thread_id):
            return {
                "from_address": "counterpart@example.com", "subject": "Re: hi",
                "message_id": "<msg1>", "references_header": None, "auto_submitted": False,
            }

        personal_inbox_tools.db.get_latest_inbound_message_in_thread = fake_get_thread
        await tools2["reply_to_email"].coroutine(thread_id="t1", body="I'll follow up by Monday")
        check("personal_inbox_tools.reply_to_email: successful reply fires the commitment hook with the resolved recipient",
              (4, "counterpart@example.com", "I'll follow up by Monday") in calls)

        calls.clear()
        personal_inbox_tools.resend_send_email = fake_resend_fail
        await tools2["reply_to_email"].coroutine(thread_id="t1", body="I'll follow up by Monday")
        check("personal_inbox_tools.reply_to_email: a failed reply does NOT fire the commitment hook", calls == [])

        config.RESEND_API_KEY = config_real_key
    finally:
        commitments.maybe_record_commitment = real_maybe_record
        email_tools._get_client = real_get_client
        personal_inbox_tools.resend_send_email = real_resend_send
        personal_inbox_tools.db.get_latest_inbound_message_in_thread = real_get_thread
        usage.check_and_consume = real_check_and_consume


async def main():
    await part1_extract_commitment()
    await part2_maybe_record_commitment()
    await part3_db_commitments()
    await part4_db_dossier_events()
    await part5_meeting_dossiers_templates()
    await part6_meeting_dossiers_orchestration()
    await part7_integration_tools_connected_calendar()
    await part8_pending_meeting_note_paragraph()
    await part9_send_hooks()

    print()
    print("=" * 70)
    total = len(failures)
    if total:
        print(f"{total} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("ALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
