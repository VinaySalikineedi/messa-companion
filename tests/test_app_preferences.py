"""Regression tests for the primary-app preference system (resolving
native-vs-connected conflicts across email/calendar/tasks) -- see
/root/.claude/plans/glowing-forging-pumpkin.md for the approved design.

Covers: db.get_app_preference's email-legacy-column fallback and
db.set_app_preference's write-through (messa/db.py); the toolkit-slug ->
category map (messa/tools/integration_tools.py); the new
set_app_preference/list_my_connected_apps orchestrator tools and the
generalized "Known about this user" prompt block + Calendar/Tasks routing
paragraphs (messa/agents/registry.py); and the connect-time auto-set/
ask-on-conflict confirmation message (messa/server.py).
"""
import asyncio
import os
import sys

sys.path.insert(0, "/home/claude/messa_build")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa import config, db, server  # noqa: E402
from messa.agents import registry  # noqa: E402
from messa.tools import integration_tools  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _user(**overrides):
    defaults = dict(user_id=1, phone_number="+15551234567", channel="cli")
    defaults.update(overrides)
    return config.UserContext(**defaults)


# ---------------------------------------------------------------------------
# Part 1: db.get_app_preference / db.set_app_preference against a fake pool
# ---------------------------------------------------------------------------

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


class PrefConn:
    def __init__(self, has_prefs_table=True, has_email_col=True,
                 pref_rows=None, default_email_provider="messa"):
        self.has_prefs_table = has_prefs_table
        self.has_email_col = has_email_col
        self.pref_rows = dict(pref_rows or {})  # {(user_id, category): preferred_app}
        self.default_email_provider = default_email_provider
        self.update_calls: list[tuple[int, str]] = []

    async def fetchval(self, sql, *args):
        s = " ".join(sql.split())
        if "information_schema.tables" in s:
            (table,) = args
            if table == "user_app_preferences":
                return self.has_prefs_table
            raise AssertionError(f"unexpected table check: {table!r}")
        if "information_schema.columns" in s:
            table, column = args
            if table == "users" and column == "default_email_provider":
                return self.has_email_col
            raise AssertionError(f"unexpected column check: {table}.{column}")
        if s.startswith("SELECT preferred_app FROM user_app_preferences"):
            user_id, category = args
            return self.pref_rows.get((user_id, category))
        if s.startswith("SELECT default_email_provider FROM users"):
            return self.default_email_provider
        raise AssertionError(f"unexpected fetchval: {sql!r}")

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO user_app_preferences"):
            user_id, category, preferred_app = args
            self.pref_rows[(user_id, category)] = preferred_app
            return {"id": 1, "user_id": user_id, "app_category": category, "preferred_app": preferred_app}
        if s.startswith("UPDATE users SET default_email_provider"):
            user_id, provider = args
            self.update_calls.append((user_id, provider))
            self.default_email_provider = provider
            return {"id": user_id, "default_email_provider": provider}
        raise AssertionError(f"unexpected fetchrow: {sql!r}")


async def part1_get_app_preference():
    db._column_cache.clear()

    # A real row wins outright, no fallback consulted at all.
    conn = PrefConn(pref_rows={(1, "calendar"): "googlecalendar"})
    install_fake_pool(conn)
    check("a real user_app_preferences row is returned directly",
          await db.get_app_preference(1, "calendar") == "googlecalendar")

    # No row, non-email category -> None (no legacy column to fall back to).
    check("no row + non-email category -> None", await db.get_app_preference(1, "tasks") is None)

    # No row, email category, legacy column present -> reads through to it.
    db._column_cache.clear()
    conn2 = PrefConn(pref_rows={}, default_email_provider="gmail")
    install_fake_pool(conn2)
    check("no row + email category falls back to users.default_email_provider",
          await db.get_app_preference(1, "email") == "gmail")

    # A real 'email' row takes priority OVER the legacy column, even if
    # they disagree -- the new table is authoritative the moment a row
    # exists.
    conn3 = PrefConn(pref_rows={(1, "email"): "outlook"}, default_email_provider="messa")
    install_fake_pool(conn3)
    check("a real 'email' row wins over a disagreeing legacy column value",
          await db.get_app_preference(1, "email") == "outlook")

    # Migration 020 not applied (no user_app_preferences table) + email +
    # legacy column present -> still falls back correctly.
    db._column_cache.clear()
    conn4 = PrefConn(has_prefs_table=False, default_email_provider="gmail")
    install_fake_pool(conn4)
    check("no user_app_preferences table at all + email -> still falls back to the legacy column",
          await db.get_app_preference(1, "email") == "gmail")

    # Neither the table nor the legacy column exist (a very old deployment) -> None, no crash.
    db._column_cache.clear()
    conn5 = PrefConn(has_prefs_table=False, has_email_col=False)
    install_fake_pool(conn5)
    check("neither table nor legacy column -> None, no crash",
          await db.get_app_preference(1, "email") is None)

    # Migration 020 not applied + non-email category -> None (nothing to fall back to at all).
    conn6 = PrefConn(has_prefs_table=False)
    install_fake_pool(conn6)
    check("no table + calendar category -> None", await db.get_app_preference(1, "calendar") is None)


async def part2_set_app_preference_write_through():
    db._column_cache.clear()

    # Non-email category: upserts the row, never touches the legacy column.
    conn = PrefConn()
    install_fake_pool(conn)
    row = await db.set_app_preference(1, "calendar", "googlecalendar")
    check("set_app_preference upserts the row", conn.pref_rows[(1, "calendar")] == "googlecalendar")
    check("non-email category never calls the legacy write-through", conn.update_calls == [])
    check("returns the upserted row", row is not None and row["preferred_app"] == "googlecalendar")

    # Email category with a value the legacy ENUM understands -> write-through fires.
    conn2 = PrefConn()
    install_fake_pool(conn2)
    await db.set_app_preference(1, "email", "gmail")
    check("email + 'gmail' (enum-valid) DOES write through to the legacy column",
          conn2.update_calls == [(1, "gmail")])
    check("legacy column value actually updated", conn2.default_email_provider == "gmail")

    # Email category with a value the legacy ENUM does NOT understand (e.g.
    # a future 'outlook' connection) -> skipped cleanly, no error, table
    # write still succeeds.
    conn3 = PrefConn()
    install_fake_pool(conn3)
    row3 = await db.set_app_preference(1, "email", "outlook")
    check("email + 'outlook' (not enum-valid) skips the legacy write-through",
          conn3.update_calls == [])
    check("the user_app_preferences row is still written for 'outlook'",
          conn3.pref_rows[(1, "email")] == "outlook")
    check("still returns successfully despite skipping the write-through", row3 is not None)

    # Migration 020 not applied -> None, and the write-through is never
    # even attempted (nothing to keep in sync with if the primary store
    # itself couldn't be written).
    conn4 = PrefConn(has_prefs_table=False)
    install_fake_pool(conn4)
    row4 = await db.set_app_preference(1, "email", "gmail")
    check("no user_app_preferences table -> returns None", row4 is None)
    check("no table -> write-through never attempted either", conn4.update_calls == [])


# ---------------------------------------------------------------------------
# Part 3: toolkit -> category map (messa/tools/integration_tools.py)
# ---------------------------------------------------------------------------

def part3_category_map():
    check("gmail -> email", integration_tools.app_category_for_toolkit("gmail") == "email")
    check("googlecalendar -> calendar", integration_tools.app_category_for_toolkit("googlecalendar") == "calendar")
    check("todoist -> tasks", integration_tools.app_category_for_toolkit("todoist") == "tasks")
    check("case-insensitive lookup", integration_tools.app_category_for_toolkit("GoogleCalendar") == "calendar")
    check("an unrelated toolkit (reddit) has no category", integration_tools.app_category_for_toolkit("reddit") is None)
    check("empty/None input doesn't crash", integration_tools.app_category_for_toolkit("") is None)
    check("reminders is deliberately NOT a mapped category for anything",
          "reminders" not in integration_tools.TOOLKIT_APP_CATEGORY.values())


# ---------------------------------------------------------------------------
# Part 4: registry.py's set_app_preference / list_my_connected_apps tools,
# end-to-end against a real build_orchestrator_tools(user) closure with
# db.* monkeypatched (same style as test_dynamic_connected_apps.py's
# part5_wiring) rather than a fake pool -- these tools call several db.*
# functions, a fake pool would just be re-testing Part 1/2 a second time.
# ---------------------------------------------------------------------------

def _install_fake_db(active_toolkits, prefs, email_connected):
    async def fake_get_active_connected_toolkits(user_id):
        return list(active_toolkits)

    async def fake_get_app_preference(user_id, category):
        return prefs.get(category)

    set_calls = []

    async def fake_set_app_preference(user_id, category, preferred_app):
        prefs[category] = preferred_app
        set_calls.append((user_id, category, preferred_app))
        return {"user_id": user_id, "app_category": category, "preferred_app": preferred_app}

    db.get_active_connected_toolkits = fake_get_active_connected_toolkits
    db.get_app_preference = fake_get_app_preference
    db.set_app_preference = fake_set_app_preference
    return set_calls


async def part4_set_app_preference_tool():
    prefs = {}
    set_calls = _install_fake_db(active_toolkits=["googlecalendar", "todoist"], prefs=prefs, email_connected=False)
    user = _user(email_connected=False)
    tools = registry.build_orchestrator_tools(user)
    set_pref = next(t for t in tools if t.name == "set_app_preference")

    bad_cat = await set_pref.coroutine(category="bogus", app="messa")
    check("rejects an invalid category", "category must be" in bad_cat)

    not_connected = await set_pref.coroutine(category="tasks", app="asana")
    check("rejects switching to an app that isn't connected", "isn't" in not_connected and "connected" in not_connected)
    check("does NOT write anything when the app isn't connected", set_calls == [])

    ok = await set_pref.coroutine(category="tasks", app="todoist")
    check("accepts switching to a connected app", "Primary tasks is now todoist" in ok)
    check("actually wrote the preference", set_calls == [(1, "tasks", "todoist")])

    ok_native = await set_pref.coroutine(category="calendar", app="messa")
    check("switching back to 'messa' (native) always succeeds -- it's always considered connected",
          "Primary calendar is now Messa's own native tool" in ok_native)

    # Gmail specifically must be checked via user.email_connected, NOT the
    # generic connected_toolkits set (it isn't in app_connection_requests
    # at all -- see registry.py's _is_app_connected docstring).
    prefs2 = {}
    _install_fake_db(active_toolkits=[], prefs=prefs2, email_connected=True)
    user_gmail = _user(email_connected=True)
    tools_gmail = registry.build_orchestrator_tools(user_gmail)
    set_pref_gmail = next(t for t in tools_gmail if t.name == "set_app_preference")
    result_gmail = await set_pref_gmail.coroutine(category="email", app="gmail")
    check("Gmail is recognized as connected via user.email_connected even with an empty generic connected set",
          "Primary email is now gmail" in result_gmail)

    user_no_gmail = _user(email_connected=False)
    tools_no_gmail = registry.build_orchestrator_tools(user_no_gmail)
    set_pref_no_gmail = next(t for t in tools_no_gmail if t.name == "set_app_preference")
    result_no_gmail = await set_pref_no_gmail.coroutine(category="email", app="gmail")
    check("Gmail correctly rejected as not-connected when user.email_connected is False",
          "isn't" in result_no_gmail and "connected" in result_no_gmail)


async def part5_list_my_connected_apps_tool():
    prefs = {"calendar": "googlecalendar"}
    _install_fake_db(active_toolkits=["googlecalendar", "reddit"], prefs=prefs, email_connected=True)
    user = _user(email_connected=True)
    tools = registry.build_orchestrator_tools(user)
    list_apps = next(t for t in tools if t.name == "list_my_connected_apps")
    result = await list_apps.coroutine()

    check("reports gmail under email (via user.email_connected, not the generic set)", "email: connected -- gmail" in result)
    check("reports googlecalendar as primary for calendar", "calendar: connected -- googlecalendar. Primary: googlecalendar." in result)
    check("tasks category correctly shows nothing connected", "tasks: nothing connected yet. Primary: messa" in result)
    check("reddit (no category) is listed separately as 'other'", "reddit" in result and "no native Messa equivalent" in result)


# ---------------------------------------------------------------------------
# Part 6: _build_system_prompt's generalized "Known about this user" block
# and the new Calendar/Tasks routing paragraphs
# ---------------------------------------------------------------------------

def part6_system_prompt_generalization():
    user = _user(email_connected=True)

    prompt_connected = registry._build_system_prompt(
        user, ["gmail", "googlecalendar", "todoist"],
        {"email": "gmail", "calendar": "googlecalendar", "tasks": "todoist"},
    )
    check("Calendar routing paragraph exists", "Calendar routing --" in prompt_connected)
    check("Tasks routing paragraph exists", "Tasks routing --" in prompt_connected)
    check("reminders are explicitly carved out of tasks routing", "Reminders are NOT part of this" in prompt_connected)
    check("known-state line shows googlecalendar as primary calendar, connected",
          "primary calendar: their connected googlecalendar (integrations_agent)" in prompt_connected)
    check("known-state line shows todoist as primary tasks, connected",
          "primary tasks: their connected todoist (integrations_agent)" in prompt_connected)
    check("email routing still mentions set_app_preference (not the old set_default_email_provider)",
          "set_app_preference('email'" in prompt_connected)
    check("old set_default_email_provider tool name is gone from the prompt",
          "set_default_email_provider" not in prompt_connected)

    # Stale-connection fallback, generalized to calendar/tasks (previously
    # email-only).
    prompt_stale = registry._build_system_prompt(
        user, [],  # nothing actually connected right now
        {"email": "messa", "calendar": "googlecalendar", "tasks": "todoist"},
    )
    check("stale calendar primary triggers the fallback-to-native wording",
          "primary calendar is set to googlecalendar, but it isn't connected right now" in prompt_stale)
    check("stale calendar fallback tells Messa to treat the native tool as effective default",
          "treat Messa's own internal calendar (executive_assistant) as the effective default" in prompt_stale)
    check("stale tasks primary triggers the same fallback wording",
          "primary tasks is set to todoist, but it isn't connected right now" in prompt_stale)

    # Default (nothing set anywhere) -> native for all three, no crash.
    prompt_default = registry._build_system_prompt(_user(), [], {})
    check("with no app_preferences at all, defaults to messa for every category",
          "primary calendar: Messa's own internal calendar (executive_assistant)" in prompt_default
          and "primary tasks: Messa's own internal task list (executive_assistant)" in prompt_default)


def part7_integrations_agent_description():
    desc_default = registry._integrations_agent_description(["reddit"])
    check("default (email_primary omitted) keeps the old flat 'NOT for email' exclusion",
          "NOT for email" in desc_default)

    desc_native_email = registry._integrations_agent_description(["reddit"], "messa")
    check("email_primary='messa' explicitly -> still excludes email",
          "NOT for email" in desc_native_email)

    desc_gmail_primary = registry._integrations_agent_description(["gmail"], "gmail")
    check("email_primary='gmail' -> no longer flatly excludes email",
          "NOT for email" not in desc_gmail_primary)
    check("email_primary='gmail' -> explains the conditional instead",
          "current primary" in desc_gmail_primary)


# ---------------------------------------------------------------------------
# Part 8: server.py's _connection_confirmation_message -- the auto-set-on-
# first-connect / ask-on-conflict / plain-reconnect-confirmation flow
# ---------------------------------------------------------------------------

async def part8_connection_confirmation_message():
    # 1. No category at all (e.g. reddit) -> completely unchanged plain message.
    prefs = {}
    _install_fake_db(active_toolkits=[], prefs=prefs, email_connected=False)
    msg_no_category = await server._connection_confirmation_message(1, "reddit", "reddit")
    check("a non-conflicting toolkit gets the plain, unchanged confirmation",
          msg_no_category == "Your reddit is connected! Just ask and I can use it now.")
    check("a non-conflicting toolkit never writes any preference", prefs == {})

    # 2. Nothing primary yet for this category -> auto-promote + say so.
    prefs2 = {}
    _install_fake_db(active_toolkits=[], prefs=prefs2, email_connected=False)
    msg_auto = await server._connection_confirmation_message(1, "googlecalendar", "Google Calendar")
    check("auto-promotes when nothing was set for the category yet",
          prefs2.get("calendar") == "googlecalendar")
    check("auto-promote message says it's now primary",
          "set as your primary calendar" in msg_auto)
    check("auto-promote message tells them how to switch back",
          "use my own calendar instead" in msg_auto)

    # 3. A DIFFERENT app already primary -> ask, don't override.
    prefs3 = {"email": "gmail"}
    _install_fake_db(active_toolkits=["gmail"], prefs=prefs3, email_connected=True)
    msg_conflict = await server._connection_confirmation_message(1, "outlook", "Outlook")
    check("a genuine conflict does NOT silently overwrite the existing primary",
          prefs3.get("email") == "gmail")
    check("conflict message names both apps and asks",
          "gmail is your primary email right now" in msg_conflict and "switch to Outlook" in msg_conflict)

    # 4. Reconnecting/re-authing the SAME app that's already primary -> plain confirmation, no question.
    prefs4 = {"tasks": "todoist"}
    _install_fake_db(active_toolkits=["todoist"], prefs=prefs4, email_connected=False)
    msg_same = await server._connection_confirmation_message(1, "todoist", "todoist")
    check("reconnecting the already-primary app gets the plain confirmation, no question",
          msg_same == "Your todoist is connected! Just ask and I can use it now.")


async def main():
    await part1_get_app_preference()
    await part2_set_app_preference_write_through()
    part3_category_map()
    await part4_set_app_preference_tool()
    await part5_list_my_connected_apps_tool()
    part6_system_prompt_generalization()
    part7_integrations_agent_description()
    await part8_connection_confirmation_message()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    else:
        print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
