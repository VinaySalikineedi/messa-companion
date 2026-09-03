"""Tests for the legacy default-email-provider PLUMBING
(migrations/015_default_email_provider.sql / users.default_email_provider
column / db.set_default_email_provider): still-real, still-used mechanisms
even after the primary-app preference system (/tmp/test_app_preferences.py)
superseded the USER-FACING side of this feature.

registry.py's set_default_email_provider TOOL is intentionally GONE --
replaced by the generalized set_app_preference('email'|'calendar'|'tasks',
app) tool, per the approved plan at
/root/.claude/plans/glowing-forging-pumpkin.md. db.set_default_email_provider
the FUNCTION lives on underneath it as a legacy write-through target (see
db.set_app_preference's own docstring) -- users.default_email_provider is
still read every turn via the free UserContext.default_email_provider field
and still needs to stay correct, which is what parts 1-3 below still
verify, unchanged. Parts 4-5 (the old tool-level and prompt-level checks)
are superseded by /tmp/test_app_preferences.py's Part 4 (set_app_preference
tool, including its email/gmail write-through) and Part 6
(_build_system_prompt's generalized "Known about this user" block) -- see
that file rather than duplicating that coverage here.

Three parts now:
  1. db.set_default_email_provider -- guarded no-op pre-migration,
     successful update, unknown user id.
  2. config.UserContext.default_email_provider -- defaults to "messa"
     whether or not a value is passed.
  3. cli._context_from_row -- propagates the column's value, and falls
     back to "messa" for a user_row that predates the migration (no key
     at all) the same way the DB column's own DEFAULT does.
"""
import asyncio
import os
import sys

sys.path.insert(0, "/home/claude/messa_build")
os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa import cli, config, db  # noqa: E402
from messa.agents import registry  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


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


class DefaultProviderConn:
    """Models exactly the users columns set_default_email_provider touches."""

    def __init__(self, users=None, has_column=True):
        self.users = users or {}  # user_id -> {"default_email_provider": ...}
        self.has_column = has_column
        self.executed_updates = []

    async def fetchval(self, sql, *args):
        if "information_schema.columns" in sql:
            return self.has_column
        raise AssertionError(f"unexpected fetchval: {sql!r}")

    async def fetchrow(self, sql, *args):
        stripped = sql.strip()
        if stripped == "UPDATE users SET default_email_provider = $2 WHERE id = $1 RETURNING *":
            user_id, provider = args
            self.executed_updates.append((user_id, provider))
            if user_id not in self.users:
                return None
            self.users[user_id]["default_email_provider"] = provider
            return dict(self.users[user_id], id=user_id)
        raise AssertionError(f"unexpected fetchrow: {sql!r}")


async def part1_db_set_default_email_provider():
    conn = DefaultProviderConn(users={1: {"default_email_provider": "messa"}})
    install_fake_pool(conn)
    row = await db.set_default_email_provider(1, "gmail")
    check("a successful update returns the updated row", row is not None and row["default_email_provider"] == "gmail")
    check("exactly one UPDATE was issued", conn.executed_updates == [(1, "gmail")])

    unknown = await db.set_default_email_provider(999, "messa")
    check("an unknown user id returns None (no row to update)", unknown is None)

    db._column_cache.clear()  # True is cached forever -- see db.py's own docstring
    conn_missing = DefaultProviderConn(has_column=False)
    install_fake_pool(conn_missing)
    result = await db.set_default_email_provider(1, "gmail")
    check("missing migration -> None, no exception, no UPDATE attempted",
          result is None and conn_missing.executed_updates == [])
    db._column_cache.clear()


def part2_user_context_default():
    check("UserContext.default_email_provider defaults to 'messa' with no kwarg",
          config.UserContext(user_id=1, phone_number="+1").default_email_provider == "messa")
    check("UserContext.default_email_provider honors an explicit value",
          config.UserContext(user_id=1, phone_number="+1", default_email_provider="gmail").default_email_provider == "gmail")


async def part3_context_from_row():
    # _context_from_row also calls ensure_timezone_resolved, ensure_default_briefings,
    # get_or_create_live_share_token, and get_or_create_messa_email_local_part -- all
    # real DB-hitting self-heal calls unrelated to what this part actually tests
    # (default_email_provider propagation). Stub them out to no-ops so this part is
    # isolated from needing a fake connection that models all four of those too.
    real_ensure_tz = db.ensure_timezone_resolved
    real_ensure_briefings = db.ensure_default_briefings
    real_live_token = db.get_or_create_live_share_token
    real_messa_email = db.get_or_create_messa_email_local_part

    async def fake_ensure_tz(user_row):
        return user_row

    async def fake_ensure_briefings(user_row):
        return None

    async def fake_live_token(user_id):
        return None

    async def fake_messa_email(user_id, name):
        return None

    db.ensure_timezone_resolved = fake_ensure_tz
    db.ensure_default_briefings = fake_ensure_briefings
    db.get_or_create_live_share_token = fake_live_token
    db.get_or_create_messa_email_local_part = fake_messa_email

    row_with_gmail = {
        "id": 1, "phone_number": "+1", "name": "Jane", "email": None, "city": None,
        "timezone": "UTC", "timezone_confirmed": True, "onboarding_step": "complete",
        "default_email_provider": "gmail",
    }
    ctx = await cli._context_from_row(row_with_gmail, "sms")
    check("a row with default_email_provider='gmail' propagates to the UserContext",
          ctx.default_email_provider == "gmail")

    row_pre_migration = {
        "id": 2, "phone_number": "+2", "name": "Bob", "email": None, "city": None,
        "timezone": "UTC", "timezone_confirmed": True, "onboarding_step": "complete",
        # no default_email_provider key at all -- migration 015 not applied yet
    }
    ctx2 = await cli._context_from_row(row_pre_migration, "sms")
    check("a row predating migration 015 (no key at all) falls back to 'messa', matching "
          "the column's own DEFAULT so behavior is identical either way",
          ctx2.default_email_provider == "messa")

    row_explicit_none = {**row_pre_migration, "id": 3, "phone_number": "+3", "default_email_provider": None}
    ctx3 = await cli._context_from_row(row_explicit_none, "sms")
    check("a row with an explicit NULL also falls back to 'messa' (not None/crash)",
          ctx3.default_email_provider == "messa")

    db.ensure_timezone_resolved = real_ensure_tz
    db.ensure_default_briefings = real_ensure_briefings
    db.get_or_create_live_share_token = real_live_token
    db.get_or_create_messa_email_local_part = real_messa_email


def _mk_user(user_id=1, email_connected=False, default_email_provider="messa"):
    return config.UserContext(
        user_id=user_id, phone_number="+15551234567", name="Jane",
        email_connected=email_connected, default_email_provider=default_email_provider,
    )


def part4_old_tool_name_intentionally_gone():
    """Confirms the migration to set_app_preference was actually completed
    (not accidentally left half-done, e.g. both tools present at once,
    which would confuse the model about which one to call) -- the positive
    coverage of its replacement lives in test_app_preferences.py's Part 4,
    not duplicated here."""
    user = _mk_user(user_id=1, email_connected=True)
    tools = registry.build_orchestrator_tools(user)
    names = {t.name for t in tools}
    check("the old set_default_email_provider tool is intentionally no longer exposed",
          "set_default_email_provider" not in names)
    check("its replacement, set_app_preference, is exposed instead",
          "set_app_preference" in names)


def part5_system_prompt_still_carries_email_routing():
    """_build_system_prompt's full "Known about this user" generalization
    (email/calendar/tasks, connected-vs-stale wording, etc.) is covered by
    test_app_preferences.py's Part 6 -- this just confirms the one thing
    still squarely in this file's scope: called with NO app_preferences at
    all (this file's own _mk_user has no way to supply any -- it only ever
    set the legacy default_email_provider field), 'email' specifically
    still degrades correctly to UserContext.default_email_provider rather
    than silently defaulting to 'messa' regardless -- i.e. the legacy
    per-turn field this whole file is about still actually flows through."""
    messa_default_user = _mk_user(default_email_provider="messa", email_connected=False)
    prompt = registry._build_system_prompt(messa_default_user)
    check("system prompt always carries the Email routing guidance paragraph",
          "Email routing" in prompt and "set_app_preference" in prompt)
    check("messa-default user, no app_preferences passed: known-about block still shows Messa's own address",
          "primary email: their own Messa address (personal_inbox_agent)" in prompt)

    gmail_default_connected = _mk_user(default_email_provider="gmail", email_connected=True)
    prompt2 = registry._build_system_prompt(gmail_default_connected)
    check("gmail-default + connected, no app_preferences passed: still degrades to the legacy "
          "column and shows gmail as primary and connected",
          "primary email: their connected gmail (email_agent)" in prompt2)

    gmail_default_disconnected = _mk_user(default_email_provider="gmail", email_connected=False)
    prompt3 = registry._build_system_prompt(gmail_default_disconnected)
    check("gmail-default but NOT connected, no app_preferences passed: still flags the mismatch "
          "and falls back to the native default",
          "isn't connected right now" in prompt3
          and "treat their own Messa address (personal_inbox_agent) as the effective default" in prompt3)


async def main() -> None:
    await part1_db_set_default_email_provider()
    part2_user_context_default()
    await part3_context_from_row()
    part4_old_tool_name_intentionally_gone()
    part5_system_prompt_still_carries_email_routing()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
