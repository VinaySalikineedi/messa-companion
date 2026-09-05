"""Tests for the db.py changes behind the new "connect my email" flow:
  1. ONBOARDING_STEPS now has a 4th real step (awaiting_email_connect)
     between awaiting_location and complete, not tacked on at the end
     past complete.
  2. save_profile_field(field='connect_email', ...) advances onboarding
     from awaiting_email_connect -> complete for both 'yes' and 'no', and
     -- the important part -- NEVER attempts to write a `connect_email`
     column (there isn't one; the real users.email_connected flag is only
     ever set later, once Composio actually confirms the connection, via
     mark_email_connected). Also re-confirms the existing name/email/city
     fields still work against the same function after this change.
  3. The new email_connection_requests functions (migrations/
     012_email_connection.sql): create/get_pending/mark_notified/
     mark_email_connected (which must also flip users.email_connected in
     the same transaction)/expire_stale/expire_single -- all against a
     hand-built fake asyncpg pool/connection, same technique as the
     existing test_default_briefings.py.
"""
import asyncio
import os
import sys

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    # No real .env in this environment (e.g. a fresh cloud sandbox) --
    # fall back to harmless dummy credentials so imports succeed and
    # fake-pool/fake-model tests can run. A real .env (e.g. the actual
    # dev machine/server) always wins untouched, since
    # messa.config's own load_dotenv() never overrides an already-set var.
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa import db  # noqa: E402

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


class FakeTransaction:
    async def __aenter__(self):
        return self

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


class ProfileFieldConn:
    """Models just enough of `users` for save_profile_field: a single row,
    plus strict SQL-shape assertions so a wrong write is a hard failure,
    not a silently-passing no-op."""

    def __init__(self, row):
        self.row = dict(row)
        self.executed = []

    async def fetchrow(self, sql, *args):
        stripped = sql.strip()
        if stripped.startswith("SELECT * FROM users WHERE id = $1"):
            return dict(self.row)
        if stripped == "SELECT messa_email_local_part, name FROM users WHERE id = $1":
            # save_profile_field('name', ...) now also provisions the
            # user's Messa email address (db.get_or_create_messa_email_
            # local_part) -- this is that lookup.
            return {"messa_email_local_part": self.row.get("messa_email_local_part"), "name": self.row.get("name")}
        raise AssertionError(f"unexpected fetchrow: {sql!r}")

    async def execute(self, sql, *args):
        stripped = sql.strip()
        self.executed.append((stripped, args))
        if stripped == "UPDATE users SET onboarding_step = $2 WHERE id = $1":
            self.row["onboarding_step"] = args[1]
        elif stripped.startswith("UPDATE users SET connect_email"):
            raise AssertionError(
                "connect_email has no column of its own -- save_profile_field must never "
                "attempt to write it"
            )
        elif stripped in (
            "UPDATE users SET city_prompt_skipped = TRUE WHERE id = $1",
            "UPDATE users SET email_prompt_skipped = TRUE WHERE id = $1",
        ):
            col = stripped.split("SET", 1)[1].split("=", 1)[0].strip()
            self.row[col] = True
        elif stripped.startswith("UPDATE users SET"):
            col = stripped.split("SET", 1)[1].split("=", 1)[0].strip()
            self.row[col] = args[1]
        else:
            raise AssertionError(f"unexpected SQL: {sql!r}")

    async def fetchval(self, sql, *args):
        if "information_schema.columns" in sql:
            return True  # pretend migration 003 (users.email) has run
        raise AssertionError(f"unexpected fetchval: {sql!r}")


def part1_onboarding_steps_shape():
    # Superseded design, kept here (updated rather than deleted) as the
    # regression check for the latest phase: city/email are no longer
    # sequential onboarding steps at all (see db.py's ONBOARDING_STEPS
    # comment) -- name is the ONLY real gate, straight to 'complete',
    # matching the "onboarded in a single text" promise. City/email are now
    # ongoing, always-answerable "profile enrichment" fields (see
    # agents/registry.py's _profile_enrichment_str), not step-gated at all.
    check("ONBOARDING_STEPS has exactly 2 steps now",
          len(db.ONBOARDING_STEPS) == 2)
    check("order is name -> complete (no more awaiting_location/awaiting_email steps)",
          db.ONBOARDING_STEPS == ["awaiting_name", "complete"])
    check("awaiting_email_connect no longer exists as a step",
          "awaiting_email_connect" not in db.ONBOARDING_STEPS)
    check("awaiting_location no longer exists as a step",
          "awaiting_location" not in db.ONBOARDING_STEPS)
    check("awaiting_email no longer exists as a step",
          "awaiting_email" not in db.ONBOARDING_STEPS)


async def part2_connect_email_field_was_removed():
    # 'connect_email' used to be a real field here; it's gone now (see
    # part1's note) -- save_profile_field should reject it outright rather
    # than silently accept and no-op it.
    try:
        await db.save_profile_field(1, "connect_email", "yes")
        check("save_profile_field('connect_email', ...) raises ValueError (field removed)", False)
    except ValueError:
        check("save_profile_field('connect_email', ...) raises ValueError (field removed)", True)

    # City/email are no longer step-gated at all -- answering 'email' while
    # still on 'awaiting_name' must still WRITE the column immediately
    # (conversational, any time), even though it correctly leaves
    # onboarding_step alone (only 'name' ever advances that).
    conn2 = ProfileFieldConn({"id": 2, "onboarding_step": "awaiting_name", "name": None})
    install_fake_pool(conn2)
    result2 = await db.save_profile_field(2, "email", "someone@example.com")
    check("email writes immediately even before onboarding_step reaches 'complete'",
          result2["email"] == "someone@example.com")
    check("answering email doesn't itself touch onboarding_step (only 'name' does)",
          result2["onboarding_step"] == "awaiting_name")


async def part3_existing_fields_still_work():
    conn = ProfileFieldConn({"id": 3, "onboarding_step": "awaiting_name", "name": None})
    install_fake_pool(conn)
    result = await db.save_profile_field(3, "name", "Vinay")
    check("field='name' still writes the name column and advances straight to 'complete' now",
          result["name"] == "Vinay" and result["onboarding_step"] == "complete")


async def part7_profile_skip_flags():
    # 'skip' on city/email no longer advances a step (there's no step left
    # to advance) -- it just sets the matching *_prompt_skipped flag so the
    # profile-enrichment prompt (agents/registry.py) stops re-suggesting a
    # field the user explicitly declined.
    conn = ProfileFieldConn({"id": 4, "onboarding_step": "complete", "name": "Vinay"})
    install_fake_pool(conn)
    result = await db.save_profile_field(4, "email", "skip")
    check("save_profile_field('email', 'skip') sets email_prompt_skipped, writes no email",
          result.get("email_prompt_skipped") is True and not result.get("email"))

    conn2 = ProfileFieldConn({"id": 5, "onboarding_step": "complete", "name": "Vinay"})
    install_fake_pool(conn2)
    result2 = await db.save_profile_field(5, "city", "skip")
    check("save_profile_field('city', 'skip') sets city_prompt_skipped, writes no city",
          result2.get("city_prompt_skipped") is True and not result2.get("city"))


class EmailConnCon:
    """Models email_connection_requests + users.email_connected together,
    gated by _has_table/_has_column the same way the real functions are."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self._next_id = 1
        self.users_email_connected: dict[int, bool] = {}
        self.has_table = True
        self.has_column = True

    async def fetchval(self, sql, *args):
        if "information_schema.tables" in sql:
            return self.has_table
        if "information_schema.columns" in sql:
            return self.has_column
        raise AssertionError(f"unexpected fetchval: {sql!r}")

    async def fetchrow(self, sql, *args):
        assert sql.strip().startswith("INSERT INTO email_connection_requests")
        user_id, connected_account_id = args
        row = {
            "id": self._next_id, "user_id": user_id, "connected_account_id": connected_account_id,
            "status": "pending", "notified_at": None, "resolved_at": None,
        }
        self.rows[self._next_id] = row
        self._next_id += 1
        return dict(row)

    async def fetch(self, sql, *args):
        stripped = sql.strip()
        if stripped.startswith("SELECT e.*, u.phone_number"):
            return [dict(r, phone_number=f"+1555{r['user_id']:07d}") for r in self.rows.values() if r["status"] == "pending"]
        if stripped.startswith("UPDATE email_connection_requests\n            SET status = 'expired'") or "SET status = 'expired'" in stripped and "RETURNING" in stripped:
            expired = []
            for r in self.rows.values():
                if r["status"] == "pending":
                    r["status"] = "expired"
                    r["resolved_at"] = "now"
                    expired.append(dict(r))
            return expired
        raise AssertionError(f"unexpected fetch: {sql!r}")

    async def execute(self, sql, *args):
        stripped = sql.strip()
        if stripped.startswith("UPDATE email_connection_requests SET notified_at"):
            (request_id,) = args
            row = self.rows.get(request_id)
            if row and row.get("notified_at") is None:
                row["notified_at"] = "now"
        elif stripped.startswith("UPDATE email_connection_requests SET status = 'active'"):
            (request_id,) = args
            self.rows[request_id]["status"] = "active"
            self.rows[request_id]["resolved_at"] = "now"
        elif stripped.startswith("UPDATE users SET email_connected = TRUE"):
            (user_id,) = args
            self.users_email_connected[user_id] = True
        elif stripped.startswith("UPDATE email_connection_requests SET status = 'expired'"):
            (request_id,) = args
            self.rows[request_id]["status"] = "expired"
            self.rows[request_id]["resolved_at"] = "now"
        else:
            raise AssertionError(f"unexpected execute: {sql!r}")

    def transaction(self):
        return FakeTransaction()


async def part4_email_connection_request_lifecycle():
    conn = EmailConnCon()
    install_fake_pool(conn)

    created = await db.create_email_connection_request(55, "ca_55")
    check("create_email_connection_request returns the new row", created is not None and created["user_id"] == 55)

    pending = await db.get_pending_email_connection_requests()
    check("the new row shows up as pending", len(pending) == 1 and pending[0]["id"] == created["id"])
    check("the pending row is joined with a phone_number", "phone_number" in pending[0])

    await db.mark_email_connected(created["id"], 55)
    check("mark_email_connected resolves the request row", conn.rows[created["id"]]["status"] == "active")
    check("mark_email_connected flips users.email_connected in the same call",
          conn.users_email_connected.get(55) is True)

    pending_after = await db.get_pending_email_connection_requests()
    check("an active request no longer shows up as pending", pending_after == [])


async def part5_expire_flows():
    conn = EmailConnCon()
    install_fake_pool(conn)
    row_a = await db.create_email_connection_request(1, "ca_1")
    row_b = await db.create_email_connection_request(2, "ca_2")

    await db.expire_email_connection_request(row_a["id"])
    check("expire_email_connection_request marks exactly that one row expired",
          conn.rows[row_a["id"]]["status"] == "expired" and conn.rows[row_b["id"]]["status"] == "pending")

    expired = await db.expire_stale_email_connection_requests()
    check("expire_stale_email_connection_requests sweeps up the remaining pending row",
          len(expired) == 1 and conn.rows[row_b["id"]]["status"] == "expired")


async def part6_missing_migration_no_ops():
    conn = EmailConnCon()
    conn.has_table = False
    conn.has_column = False
    install_fake_pool(conn)
    result = await db.create_email_connection_request(1, "ca_1")
    check("create_email_connection_request no-ops (returns None) when migration 012 isn't applied",
          result is None)
    pending = await db.get_pending_email_connection_requests()
    check("get_pending_email_connection_requests returns [] when migration 012 isn't applied",
          pending == [])
    # Must not raise even though the table/column genuinely don't exist.
    await db.mark_email_connected(1, 1)
    await db.expire_email_connection_request(1)
    await db.expire_stale_email_connection_requests()
    check("mark_email_connected/expire_* are all silent no-ops pre-migration (no exception raised)", True)


async def main() -> None:
    part1_onboarding_steps_shape()
    await part2_connect_email_field_was_removed()
    await part3_existing_fields_still_work()
    await part4_email_connection_request_lifecycle()
    await part5_expire_flows()
    await part6_missing_migration_no_ops()
    await part7_profile_skip_flags()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
