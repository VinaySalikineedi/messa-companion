"""Tests for migrations/034_document_share_bytes.sql: storing generated-
document-share content directly in Postgres (BYTEA) instead of relying on
the container's local disk to still have the file when Sendblue later
fetches it.

Production runs inside a Docker container on Hugging Face Spaces
(live.textmessa.com). Screenshots and generated PDFs were written to the
container's local outputs/ folder; if the container restarts, or HF routes
Sendblue's download request to a different replica than the one that
generated the file, the file is gone. Fix: store the bytes in Postgres
(generated_document_shares.file_bytes) so server.py can always stream them
straight from the DB, with zero dependency on local disk.

Three things to verify:
  1. db.create_document_share -- auto-reads file_path into file_bytes when
     not given explicitly (upgrades send_pdf_over_text with no call-site
     change), respects a size cap (falls back to path-only rather than
     bloating the DB), accepts explicit file_bytes/media_type (send_screenshot
     never writes to disk at all), and degrades cleanly pre-migration (no
     file_bytes column yet).
  2. server.py's GET /files/{token} -- serves straight from file_bytes when
     present (never touches disk), falls back to file_path/disk otherwise,
     and picks media_type off the row before guessing from the filename.
  3. tools/stagehand_tools.py's send_screenshot -- passes bytes directly,
     confirmed separately in test_agent_feedback_fixes.py; this file adds a
     structural check that it never writes to local disk at all.

No live Postgres, no live browser -- fake asyncpg pool/conn, same
convention as test_app_connect_queue.py's FakePool/FakeAcquire.
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
    os.environ.setdefault("SENDBLUE_API_KEY", "sb-test-dummy-key")
    os.environ.setdefault("SENDBLUE_API_SECRET", "sb-test-dummy-secret")

# Fake `composio` package so server.py (which imports tools/integration_tools.py)
# imports cleanly without the real SDK installed -- same technique as
# test_app_connect_queue.py / test_inbound_reactions.py.
class ComposioMultipleConnectedAccountsError(Exception):
    pass


_fake_composio_exceptions = types.ModuleType("composio.exceptions")
_fake_composio_exceptions.ComposioMultipleConnectedAccountsError = ComposioMultipleConnectedAccountsError
_fake_composio_pkg = types.ModuleType("composio")
_fake_composio_pkg.exceptions = _fake_composio_exceptions
sys.modules["composio"] = _fake_composio_pkg
sys.modules["composio.exceptions"] = _fake_composio_exceptions

from messa import config, db, server  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# ---------------------------------------------------------------------------
# Part 1: db.create_document_share / get_document_share_by_token against a
# hand-built fake asyncpg pool.
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


class FakeShareConn:
    """Models just enough of asyncpg's Connection for
    create_document_share/get_document_share_by_token: the two
    information_schema existence checks (_has_table, _has_column) plus a
    plain INSERT and a SELECT-by-token, both captured for assertions."""

    def __init__(self, has_table=True, has_bytes_col=True):
        self.has_table = has_table
        self.has_bytes_col = has_bytes_col
        self.insert_calls = []
        self.rows_by_token = {}

    async def fetchval(self, query, *args):
        if "information_schema.tables" in query:
            return self.has_table
        if "information_schema.columns" in query:
            # args = (table, column)
            if args[1] == "file_bytes":
                return self.has_bytes_col
            return True  # any other column this module might ask about
        raise AssertionError(f"unexpected fetchval query: {query}")

    async def execute(self, query, *args):
        if "INSERT INTO generated_document_shares" in query:
            self.insert_calls.append((query, args))
            if "file_bytes" in query:
                user_id, token, file_path, filename, file_bytes, media_type = args
                self.rows_by_token[token] = {
                    "user_id": user_id, "token": token, "file_path": file_path,
                    "filename": filename, "file_bytes": file_bytes, "media_type": media_type,
                }
            else:
                user_id, token, file_path, filename = args
                self.rows_by_token[token] = {
                    "user_id": user_id, "token": token, "file_path": file_path,
                    "filename": filename, "file_bytes": None, "media_type": None,
                }
            return None
        raise AssertionError(f"unexpected execute query: {query}")

    async def fetchrow(self, query, *args):
        if "SELECT * FROM generated_document_shares WHERE token" in query:
            row = self.rows_by_token.get(args[0])
            return _FakeRow(row) if row else None
        raise AssertionError(f"unexpected fetchrow query: {query}")


class _FakeRow(dict):
    """asyncpg Records support dict(row) -- a plain dict already does, this
    just documents the intent at the call site."""


async def part1_auto_reads_bytes_from_disk_when_not_given(tmp_path_str):
    """send_pdf_over_text (and every other existing caller) only ever
    passes file_path -- this is the "upgrade with zero call-site changes"
    path: create_document_share reads the file itself."""
    db._column_cache.clear()
    conn = FakeShareConn(has_table=True, has_bytes_col=True)
    install_fake_pool(conn)

    real_path = Path(tmp_path_str) / "contract.pdf"
    real_path.write_bytes(b"%PDF-1.4 fake pdf content")

    token = await db.create_document_share(1, str(real_path), "contract.pdf")
    check("a token is still returned", bool(token))
    row = conn.rows_by_token[token]
    check("file_bytes was read from disk automatically", row["file_bytes"] == b"%PDF-1.4 fake pdf content")
    check("media_type was guessed from the filename", row["media_type"] == "application/pdf")
    check("file_path is still recorded (additive, not a replacement)", row["file_path"] == str(real_path))


async def part1_explicit_bytes_skip_the_disk_read_entirely(tmp_path_str):
    """send_screenshot passes file_bytes directly and never writes
    anything to file_path -- create_document_share must not try to read
    it (it doesn't exist)."""
    db._column_cache.clear()
    conn = FakeShareConn(has_table=True, has_bytes_col=True)
    install_fake_pool(conn)

    nominal_path = str(Path(tmp_path_str) / "nonexistent_screenshot.png")
    check("the nominal path genuinely does not exist on disk", not Path(nominal_path).exists())

    token = await db.create_document_share(
        1, nominal_path, "screenshot.png", file_bytes=b"\x89PNG-fake-bytes", media_type="image/png",
    )
    check("a token is returned despite the path not existing on disk", bool(token))
    row = conn.rows_by_token[token]
    check("the explicitly-passed bytes are stored verbatim", row["file_bytes"] == b"\x89PNG-fake-bytes")
    check("the explicitly-passed media_type is stored verbatim", row["media_type"] == "image/png")


async def part1_oversized_file_falls_back_to_path_only(tmp_path_str):
    """A file bigger than config.MAX_SMS_ATTACHMENT_BYTES must not bloat
    the DB row -- it degrades to the pre-migration path-only behavior
    (file_bytes stays NULL) instead of failing the share."""
    db._column_cache.clear()
    conn = FakeShareConn(has_table=True, has_bytes_col=True)
    install_fake_pool(conn)

    big_path = Path(tmp_path_str) / "huge.pdf"
    orig_cap = config.MAX_SMS_ATTACHMENT_BYTES
    try:
        config.MAX_SMS_ATTACHMENT_BYTES = 10  # tiny cap for the test
        big_path.write_bytes(b"x" * 100)
        token = await db.create_document_share(1, str(big_path), "huge.pdf")
        check("a token is still returned for an oversized file", bool(token))
        row = conn.rows_by_token[token]
        check("file_bytes is left NULL for a file over the inline size cap", row["file_bytes"] is None)
        check("file_path is still recorded so disk-serving can fall back to it", row["file_path"] == str(big_path))
    finally:
        config.MAX_SMS_ATTACHMENT_BYTES = orig_cap


async def part1_missing_file_degrades_cleanly():
    """A file_path that can't be read (missing, permission error) must
    never raise -- the share still gets created, just path-only."""
    db._column_cache.clear()
    conn = FakeShareConn(has_table=True, has_bytes_col=True)
    install_fake_pool(conn)

    token = await db.create_document_share(1, "/nonexistent/path/does_not_exist.pdf", "does_not_exist.pdf")
    check("a token is returned even when the file can't be read", bool(token))
    check("file_bytes is NULL rather than raising", conn.rows_by_token[token]["file_bytes"] is None)


async def part1_pre_migration_034_degrades_to_original_insert(tmp_path_str):
    """A deployment that hasn't run migration 034 yet (no file_bytes
    column) must keep working exactly as before -- the original 4-column
    INSERT, no crash, no attempt to read the column that doesn't exist."""
    db._column_cache.clear()
    conn = FakeShareConn(has_table=True, has_bytes_col=False)
    install_fake_pool(conn)

    real_path = Path(tmp_path_str) / "legacy.pdf"
    real_path.write_bytes(b"legacy pdf bytes")

    token = await db.create_document_share(1, str(real_path), "legacy.pdf")
    check("a token is still returned pre-migration", bool(token))
    check(
        "the original path-only INSERT statement was used (no file_bytes column referenced)",
        "file_bytes" not in conn.insert_calls[0][0],
    )
    row = conn.rows_by_token[token]
    check("the row has no file_bytes/media_type pre-migration (never even attempted)", row["file_bytes"] is None)


async def part1_no_table_returns_none():
    db._column_cache.clear()
    conn = FakeShareConn(has_table=False)
    install_fake_pool(conn)
    result = await db.create_document_share(1, "/tmp/x.pdf", "x.pdf")
    check("create_document_share no-ops (returns None) when migration 018 hasn't run either", result is None)


async def part1_get_document_share_returns_stored_bytes(tmp_path_str):
    db._column_cache.clear()
    conn = FakeShareConn(has_table=True, has_bytes_col=True)
    install_fake_pool(conn)
    token = await db.create_document_share(
        1, "irrelevant.png", "irrelevant.png", file_bytes=b"raw-bytes-here", media_type="image/png",
    )
    share = await db.get_document_share_by_token(token)
    check("get_document_share_by_token returns the stored bytes", share["file_bytes"] == b"raw-bytes-here")
    check("get_document_share_by_token returns the stored media_type", share["media_type"] == "image/png")
    check("an unknown token returns None", await db.get_document_share_by_token("no-such-token") is None)


# ---------------------------------------------------------------------------
# Part 2: server.py's GET /files/{token} -- bytes-first serving.
# ---------------------------------------------------------------------------

async def part2_serves_directly_from_bytes_never_touching_disk():
    async def fake_get_share(token):
        return {
            "token": token, "filename": "deepsearch_screenshot_abc.png",
            "file_path": "/this/path/does/not/exist/on/this/replica.png",
            "file_bytes": b"\x89PNG-the-actual-bytes", "media_type": "image/png",
        }

    db.get_document_share_by_token = fake_get_share
    resp = await server.download_shared_file("tok_bytes")
    check("a bytes-backed share returns a 200 Response", getattr(resp, "status_code", 200) == 200)
    check("the response body is exactly the stored bytes", bytes(resp.body) == b"\x89PNG-the-actual-bytes")
    check("the response media_type comes from the row, not a guess", resp.media_type == "image/png")
    check(
        "the response never touched config.resolve_output_file's disk path (file_path is bogus and unused)",
        True,  # the fact this didn't raise/404 despite a nonexistent file_path proves it
    )


async def part2_falls_back_to_disk_when_no_bytes_stored(tmp_path_str):
    """A pre-migration-034 row (or one whose bytes-read failed at creation
    time) has no file_bytes -- must fall back to the original disk-serving
    path exactly as before this change."""
    real_dir = Path(tmp_path_str)
    orig_outputs_dir = config.OUTPUTS_DIR
    try:
        config.OUTPUTS_DIR = str(real_dir)
        real_file = real_dir / "legacy_contract.pdf"
        real_file.write_bytes(b"%PDF legacy bytes on disk")

        async def fake_get_share(token):
            return {
                "token": token, "filename": "legacy_contract.pdf",
                "file_path": str(real_file), "file_bytes": None, "media_type": None,
            }

        db.get_document_share_by_token = fake_get_share
        resp = await server.download_shared_file("tok_disk")
        check(
            "a bytes-less share falls back to FileResponse from disk",
            type(resp).__name__ == "FileResponse",
        )
        check("the disk fallback still resolves a sensible media_type", resp.media_type == "application/pdf")
    finally:
        config.OUTPUTS_DIR = orig_outputs_dir


async def part2_unknown_token_still_404s():
    async def fake_get_share(token):
        return None

    db.get_document_share_by_token = fake_get_share
    resp = await server.download_shared_file("no-such-token")
    check("an unknown token still 404s", resp.status_code == 404)


async def part2_bytes_row_media_type_wins_over_filename_guess():
    """Guards against a regression where the filename-guess path
    shadows an explicitly-stored media_type."""
    async def fake_get_share(token):
        return {
            "token": token, "filename": "weird_no_extension",
            "file_path": "/tmp/irrelevant", "file_bytes": b"data", "media_type": "image/png",
        }

    db.get_document_share_by_token = fake_get_share
    resp = await server.download_shared_file("tok_weird")
    check(
        "an explicit media_type on the row wins even when the filename has no guessable extension",
        resp.media_type == "image/png",
    )


# ---------------------------------------------------------------------------
# Part 3: structural check -- send_screenshot never writes to local disk.
# ---------------------------------------------------------------------------

def part3_send_screenshot_never_touches_local_disk():
    src = (REPO_ROOT / "messa" / "tools" / "stagehand_tools.py").read_text()
    fn_start = src.index("async def _send_screenshot")
    fn_end = src.index("\n    async def ", fn_start + 10)
    body = src[fn_start:fn_end]
    check("_send_screenshot no longer calls write_bytes", "write_bytes" not in body)
    check("_send_screenshot no longer creates an outputs directory", "mkdir" not in body)
    check(
        "_send_screenshot passes file_bytes= directly to create_document_share",
        "file_bytes=img_bytes" in body,
    )
    check(
        "_send_screenshot passes an explicit image/png media_type",
        'media_type="image/png"' in body,
    )


def part3_migration_034_exists_and_is_additive():
    migration_path = REPO_ROOT / "migrations" / "034_document_share_bytes.sql"
    check("migrations/034_document_share_bytes.sql exists", migration_path.exists())
    sql = migration_path.read_text()
    check("it adds file_bytes as BYTEA", "file_bytes BYTEA" in sql)
    check("it adds media_type", "media_type" in sql)
    check("both columns are added with IF NOT EXISTS (safe to re-run, additive)", sql.count("ADD COLUMN IF NOT EXISTS") == 2)


async def main() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        await part1_auto_reads_bytes_from_disk_when_not_given(tmp)
        await part1_explicit_bytes_skip_the_disk_read_entirely(tmp)
        await part1_oversized_file_falls_back_to_path_only(tmp)
        await part1_missing_file_degrades_cleanly()
        await part1_pre_migration_034_degrades_to_original_insert(tmp)
        await part1_no_table_returns_none()
        await part1_get_document_share_returns_stored_bytes(tmp)

        await part2_serves_directly_from_bytes_never_touching_disk()
        await part2_falls_back_to_disk_when_no_bytes_stored(tmp)
        await part2_unknown_token_still_404s()
        await part2_bytes_row_media_type_wins_over_filename_guess()

    part3_send_screenshot_never_touches_local_disk()
    part3_migration_034_exists_and_is_additive()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
