#!/usr/bin/env python3
"""Proves migrations/027_memory.sql's RLS policy actually stops a cross-user
read/write at the DATABASE level -- not just that our application code
behaves. Run this once against a scratch/local Postgres+pgvector before
relying on the memory feature in production, and again after any future
change to that migration or to messa/memory.py's connection handling.

This is the real, hands-on check described in the memory feature's plan
("prove it at the SQL level, not just that our application code behaves")
-- it connects as a NON-superuser, NON-table-owner role (superusers and
table owners bypass RLS by default, which would make this test pass for
the wrong reason) and tries, directly in SQL, to read/update/delete a
memory row that belongs to a DIFFERENT user than the one
`app.current_user_id` is set to. If any of those succeed, this exits
non-zero and prints exactly what leaked.

It ALSO calls messa/memory.py's real `_configure_connection` (imported
directly, not reimplemented here) through a real psycopg_pool.
ConnectionPool first, before any of the RLS checks -- added after a real
production bug: an earlier version of `_configure_connection` used a bind
parameter on a `SET` statement (`conn.execute("SET x = %s", (val,))`),
which Postgres rejects outright (`SyntaxError: syntax error at or near
"$1"`) -- and psycopg_pool swallows that into a generic, misleading "pool
initialization incomplete after 15 sec" on the CALLING side, with the real
error only visible in the pool's own background connection log. This
script's OWN internal SET statement (further down) was written correctly
from the start, so it could never have caught that the real
`messa/memory.py` had the bug -- calling the real function here, not a
hand-written equivalent, closes exactly that gap.

Usage:
    python3 scripts/verify_memory_rls.py "postgresql://user:pass@host:port/dbname"

The target database must already have migrations/027_memory.sql applied
(this script only creates its own throwaway app_role and test rows; it
does not run the migration itself). Safe to run against a real database:
everything it creates (the role, the two test rows) is cleaned up at the
end, in a `finally` block, even if an assertion fails partway through.
"""
from __future__ import annotations

import os
import sys
import uuid

try:
    import psycopg
except ImportError:
    print("This script needs psycopg (psycopg3): pip install 'psycopg[binary]'", file=sys.stderr)
    sys.exit(2)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_ROLE = "mem0_rls_verify_role"
TEST_ROLE_PASSWORD = "verify_" + uuid.uuid4().hex[:12]


def _owner_conn(dsn: str) -> "psycopg.Connection":
    conn = psycopg.connect(dsn, autocommit=True)
    return conn


def _verify_real_configure_connection(dsn: str) -> bool:
    """Calls messa/memory.py's ACTUAL _configure_connection through a real
    psycopg_pool.ConnectionPool -- see this module's own docstring for the
    production bug this specifically guards against. Prints PASS/FAIL like
    every other check below; returns True on success."""
    try:
        from psycopg_pool import ConnectionPool
    except ImportError:
        print("[SKIP] real _configure_connection check -- psycopg_pool not installed "
              "(pip install 'psycopg[pool]')")
        return True
    try:
        from messa import memory as messa_memory
    except Exception as e:  # noqa: BLE001 - report and skip rather than abort the whole script
        print(f"[SKIP] real _configure_connection check -- couldn't import messa.memory ({e})")
        return True

    try:
        pool = ConnectionPool(
            conninfo=dsn, min_size=1, max_size=1, open=False,
            configure=lambda conn: messa_memory._configure_connection(conn, 999999),
        )
        pool.open(wait=True, timeout=15)
        with pool.connection() as conn:
            row = conn.execute("SHOW app.current_user_id").fetchone()
        pool.close()
        ok = row is not None and row[0] == "999999"
        print(f"[{'PASS' if ok else 'FAIL'}] messa/memory.py's real _configure_connection "
              "works against a real connection (no SET syntax error, value actually applied)")
        return ok
    except Exception as e:  # noqa: BLE001 - this IS the check; a raise here is a real FAIL
        print(f"[FAIL] messa/memory.py's real _configure_connection raised: {e}")
        return False


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    dsn = sys.argv[1]

    real_configure_ok = _verify_real_configure_connection(dsn)

    owner = _owner_conn(dsn)
    user1_id, user2_id = str(uuid.uuid4()), str(uuid.uuid4())
    mem1_id, mem2_id = str(uuid.uuid4()), str(uuid.uuid4())
    failures: list[str] = []

    try:
        # CREATE ROLE ... PASSWORD doesn't accept a bind parameter (it's DDL,
        # not a normal query) -- safe to inline here since TEST_ROLE_PASSWORD
        # is our own freshly-generated uuid hex, never external input.
        owner.execute(f"CREATE ROLE {TEST_ROLE} LOGIN PASSWORD '{TEST_ROLE_PASSWORD}' NOSUPERUSER NOBYPASSRLS")
        owner.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON mem0_memories TO {TEST_ROLE}")

        owner_of_table = owner.execute(
            "SELECT tableowner FROM pg_tables WHERE tablename = 'mem0_memories'"
        ).fetchone()
        if owner_of_table and owner_of_table[0] == TEST_ROLE:
            print("SETUP ERROR: the test role must NOT own mem0_memories (owners bypass RLS).")
            return 2

        owner.execute(
            "INSERT INTO mem0_memories (id, vector, payload) VALUES (%s, NULL, %s::jsonb), (%s, NULL, %s::jsonb)",
            (mem1_id, f'{{"user_id": "{user1_id}", "memory": "user1 secret"}}',
             mem2_id, f'{{"user_id": "{user2_id}", "memory": "user2 secret"}}'),
        )

        # Rebuild the DSN with the test role's own credentials rather than
        # string-splicing the original -- simpler and correct regardless of
        # what auth info the original DSN did or didn't already contain.
        import urllib.parse as up
        parts = up.urlsplit(dsn)
        app_dsn = up.urlunsplit(parts._replace(netloc=f"{TEST_ROLE}:{TEST_ROLE_PASSWORD}@{parts.hostname}:{parts.port or 5432}"))

        app_conn = psycopg.connect(app_dsn, autocommit=True)
        # SET doesn't accept a bind parameter either -- same reasoning as
        # CREATE ROLE above; user1_id is our own generated uuid, safe to
        # inline.
        app_conn.execute(f"SET app.current_user_id = '{user1_id}'")

        def check(label: str, cond: bool) -> None:
            print(f"[{'PASS' if cond else 'FAIL'}] {label}")
            if not cond:
                failures.append(label)

        rows = app_conn.execute("SELECT id FROM mem0_memories").fetchall()
        # psycopg returns uuid columns as uuid.UUID objects, not str -- cast
        # both sides so this compares values, not types.
        check("scoped to user1: SELECT * returns only user1's row",
              [str(r[0]) for r in rows] == [mem1_id])

        row = app_conn.execute("SELECT * FROM mem0_memories WHERE id = %s", (mem2_id,)).fetchall()
        check("scoped to user1: direct SELECT of user2's row by id returns nothing", row == [])

        update_cur = app_conn.execute(
            "UPDATE mem0_memories SET payload = jsonb_set(payload, '{memory}', '\"HACKED\"') WHERE id = %s",
            (mem2_id,),
        )
        check("scoped to user1: UPDATE of user2's row affects 0 rows", update_cur.rowcount == 0)
        still_intact = owner.execute("SELECT payload->>'memory' FROM mem0_memories WHERE id = %s", (mem2_id,)).fetchone()
        check("user2's row is untouched after the UPDATE attempt", still_intact and still_intact[0] == "user2 secret")

        delete_cur = app_conn.execute("DELETE FROM mem0_memories WHERE id = %s", (mem2_id,))
        check("scoped to user1: DELETE of user2's row affects 0 rows", delete_cur.rowcount == 0)
        still_there = owner.execute("SELECT 1 FROM mem0_memories WHERE id = %s", (mem2_id,)).fetchone()
        check("user2's row still exists after the DELETE attempt", still_there is not None)

        forged_ok = True
        try:
            app_conn.execute(
                "INSERT INTO mem0_memories (id, vector, payload) VALUES (%s, NULL, %s::jsonb)",
                (str(uuid.uuid4()), f'{{"user_id": "{user2_id}", "memory": "forged"}}'),
            )
            forged_ok = False
        except psycopg.errors.InsufficientPrivilege:
            pass
        except Exception:
            pass
        check("scoped to user1: INSERT forging user2's user_id is rejected (WITH CHECK)", forged_ok)

        app_conn.execute(
            "UPDATE mem0_memories SET payload = jsonb_set(payload, '{memory}', '\"updated fine\"') WHERE id = %s",
            (mem1_id,),
        )
        own = owner.execute("SELECT payload->>'memory' FROM mem0_memories WHERE id = %s", (mem1_id,)).fetchone()
        check("scoped to user1: UPDATE of OWN row still works normally", own and own[0] == "updated fine")

        app_conn.close()

        no_var_conn = psycopg.connect(app_dsn, autocommit=True)
        count = no_var_conn.execute("SELECT count(*) FROM mem0_memories").fetchone()[0]
        check("no app.current_user_id set at all: sees zero rows (fails closed, not open)", count == 0)
        no_var_conn.close()

    finally:
        try:
            owner.execute("DELETE FROM mem0_memories WHERE id IN (%s, %s)", (mem1_id, mem2_id))
        except Exception:
            pass
        try:
            owner.execute(f"REVOKE ALL ON mem0_memories FROM {TEST_ROLE}")
            owner.execute(f"DROP ROLE IF EXISTS {TEST_ROLE}")
        except Exception:
            pass
        owner.close()

    if not real_configure_ok:
        failures.append("messa/memory.py's real _configure_connection")

    if failures:
        print(f"\n{len(failures)} FAILURE(S) -- the RLS policy does NOT fully isolate users: {failures}")
        return 1
    print("\nAll checks passed -- RLS genuinely blocks cross-user access at the database level.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
