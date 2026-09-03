"""Messa's two-tier memory, on top of Mem0 + Postgres/pgvector, with Row-
Level Security as a real, database-enforced safety backstop.

Why this module exists, separate from just calling mem0ai directly: Mem0's
own source (checked directly, not assumed -- see migrations/027_memory.sql's
own comments) has a real gap. `search()`/`get_all()` require and enforce a
`user_id` filter, but `update()`/`delete()`/`history()` take a bare
`memory_id` with NO `user_id` parameter and NO ownership check at all --
Mem0 just trusts whatever memory_id the caller passes. Left alone, one wrong
memory_id anywhere in OUR OWN code (a mixed-up variable under concurrent
async requests, say) would silently let one user's turn touch another
user's memory. This module closes that gap two ways:

  1. Every single call into Mem0 runs through a dedicated, single-
     connection, single-use Postgres connection pool (`_run_scoped`, min
     size 1, max size 1, opened right before the call and closed right
     after) against a session variable (`app.current_user_id`) set ONCE at
     that one physical connection's creation. The RLS policy on
     mem0_memories (migrations/027_memory.sql) checks that variable on
     every row it touches, at the database level -- so even a wrong
     memory_id in our own code literally cannot reach another user's row,
     regardless of what Python code asked for it. This also means no
     shared, long-lived Mem0 instance ever exists across users or
     requests, which independently addresses the "shared instance at
     scale on a 16GB CPU box" concern raised while evaluating Mem0.

     This has to be a DIRECT (unpooled) Postgres connection
     (config.MESSA_MEM0_DIRECT_DATABASE_URL), not the app's normal pooled
     DATABASE_URL -- under Neon's PgBouncer transaction-pooling mode (see
     db.py's own comments on why DATABASE_URL needs asyncpg's
     statement_cache_size=0), a session-level SET is not guaranteed to
     still be in effect for the next transaction on "the same" client
     connection. A direct connection has one real, stable backend session
     for as long as we hold it, so the SET is guaranteed to still apply.

  2. For update/delete/history specifically -- the three methods Mem0
     itself doesn't check -- this module verifies ownership itself before
     ever calling them (`_guarded_mutation`): it reads the memory back
     under the SAME per-user RLS-scoped connection first. If RLS means
     that read comes back empty (the row isn't visible under this
     user_id -- either it belongs to someone else, or the memory_id is
     just wrong), this doesn't quietly give up: it logs a structured
     incident, re-derives user_id ONCE more from the one thing genuinely
     trusted -- a fresh phone-number lookup via db.get_user_by_phone, NOT
     from the memory_id, Mem0's own metadata, or anything about the failed
     call itself -- and retries exactly once. If that also fails, it fails
     closed: tells the caller the memory wasn't found, never widens the
     query or drops the filter.

Two-tier design (see README's "Memory" section for the full why):

  - The ALWAYS-ON, cheap tier is NOT Mem0 at all -- it's
    users.memory_profile, a plain column read every turn through the
    app's normal asyncpg pool (see config.UserContext.memory_profile /
    cli._context_from_row). This module never gets called on that hot
    path.
  - Mem0 (everything in this module) is touched in exactly two places:
    `run_daily_memory_batch` below (the once-a-day batch job that writes
    episodic memory AND regenerates the profile digest) and
    `search_recent_context` (the on-demand recall_past_conversation tool,
    called only when the model decides it's actually needed -- see
    agents/registry.py).

Feature flag: everything in this module no-ops cleanly (returns a plain
"memory isn't set up yet" result, never raises) whenever
config.MEM0_ENABLED is False -- i.e. MESSA_MEM0_DIRECT_DATABASE_URL isn't
set. Nothing else in the app depends on this being configured.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import config, console, db

try:
    from psycopg_pool import ConnectionPool
    _PSYCOPG_AVAILABLE = True
except ImportError:  # pragma: no cover - only true if the dependency truly isn't installed
    ConnectionPool = None  # type: ignore[assignment]
    _PSYCOPG_AVAILABLE = False

try:
    from mem0 import Memory
    _MEM0_AVAILABLE = True
except ImportError:  # pragma: no cover - same reasoning as psycopg above
    Memory = None  # type: ignore[assignment]
    _MEM0_AVAILABLE = False


NOT_CONFIGURED = "Memory isn't set up yet (MESSA_MEM0_DIRECT_DATABASE_URL not configured)."


def _available() -> bool:
    return bool(config.MEM0_ENABLED and _PSYCOPG_AVAILABLE and _MEM0_AVAILABLE)


def _mem0_config(pool: "ConnectionPool") -> dict[str, Any]:
    """Mem0's config dict for one already-built, already-RLS-scoped pool.

    llm: provider "openai" (not a typo -- see module docstring/README) --
    Mem0's OpenAILLM class auto-detects OPENROUTER_API_KEY in the process
    environment and switches its base_url to OpenRouter's own, confirmed
    directly against Mem0's source. Reuses config.OPENROUTER_API_KEY as-is;
    no new key/dependency for the LLM side.

    embedder: provider "openai" pointed explicitly at OpenRouter's
    /v1/embeddings endpoint (Mem0's embedder class, unlike its LLM class,
    does NOT auto-detect OpenRouter -- confirmed directly against source --
    so api_key/openai_base_url are passed explicitly here rather than
    relying on env detection). This is the one part of this design not yet
    verified live (this sandbox's network can't reach OpenRouter or
    huggingface.co) -- see config.MEM0_EMBEDDER_PROVIDER/README's "Memory"
    section for the fastembed fallback if this doesn't pan out."""
    if config.MEM0_EMBEDDER_PROVIDER == "fastembed":
        embedder = {
            "provider": "fastembed",
            "config": {"model": "BAAI/bge-small-en-v1.5"},
        }
    else:
        embedder = {
            "provider": "openai",
            "config": {
                "api_key": config.OPENROUTER_API_KEY,
                "openai_base_url": "https://openrouter.ai/api/v1",
                "embedding_dims": config.MEM0_EMBEDDING_DIMS,
            },
        }
    return {
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "collection_name": config.MEM0_COLLECTION_NAME,
                "embedding_model_dims": config.MEM0_EMBEDDING_DIMS,
                "connection_pool": pool,
                "hnsw": True,
            },
        },
        "llm": {
            "provider": "openai",
            "config": {"model": "openai/gpt-4o-mini"},
        },
        "embedder": embedder,
    }


def _configure_connection(conn, user_id: int) -> None:
    """Runs exactly once, at this pool's one physical connection's
    creation (psycopg_pool's `configure` callback -- NOT re-run on every
    checkout/checkin). autocommit=True so the SET below takes effect
    immediately and stays in effect for the life of this connection
    without needing an explicit commit that could otherwise interact
    oddly with Mem0's own per-operation transactions."""
    conn.autocommit = True
    conn.execute("SET app.current_user_id = %s", (str(user_id),))


def _run_scoped_sync(user_id: int, fn: Callable[[Any], Any]) -> Any:
    """Builds a dedicated, single-connection, RLS-scoped Mem0 client for
    exactly one operation, runs fn(memory) against it, and always closes
    the pool afterward -- see module docstring for the full "why". This is
    the ONE place a physical Postgres connection for Mem0 gets opened
    anywhere in this app; nothing holds one open longer than a single
    call."""
    pool = ConnectionPool(
        conninfo=config.MESSA_MEM0_DIRECT_DATABASE_URL,
        min_size=1,
        max_size=1,
        open=False,
        configure=lambda conn: _configure_connection(conn, user_id),
    )
    pool.open(wait=True, timeout=15)
    try:
        memory = Memory.from_config(_mem0_config(pool))
        return fn(memory)
    finally:
        pool.close()


async def _run_scoped(user_id: int, fn: Callable[[Any], Any]) -> Any:
    """Async wrapper -- the whole blocking sequence (open pool, build
    Mem0, run fn, close pool) happens in one worker thread via
    asyncio.to_thread, same as how Mem0's own AsyncMemory wraps its sync
    vector store internally (confirmed directly against source -- it's
    not a separate async driver, so this is the same shape, just made
    explicit here instead of going through AsyncMemory a second time)."""
    return await asyncio.to_thread(_run_scoped_sync, user_id, fn)


def _log_incident(op: str, caller_user_id: int, resolved_user_id: int | None, memory_id: str, reason: str) -> None:
    """Structured (single-line, greppable) incident log -- shaped so a
    future admin-dashboard incident feed (explicitly out of scope for this
    round -- see the plan) could read these later without a format change.
    Never raises; logging failure should never take down the actual
    fail-closed behavior around it."""
    payload = {
        "event": "memory_ownership_guard_blocked",
        "op": op,
        "caller_user_id": caller_user_id,
        "resolved_user_id": resolved_user_id,
        "memory_id": memory_id,
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    console.system(f"MEMORY_INCIDENT: {json.dumps(payload)}")


async def _trusted_user_id(user: config.UserContext) -> int:
    """Re-derives user_id from the one thing genuinely trusted every
    turn -- a fresh phone-number lookup -- rather than trusting whatever
    int happens to be sitting in `user.user_id` at the moment of a retry.
    Falls back to user.user_id if the phone number somehow doesn't
    resolve (should not happen in practice; better to proceed with the
    original value than crash the retry itself)."""
    row = await db.get_user_by_phone(user.phone_number)
    return row["id"] if row else user.user_id


async def _guarded_mutation(user: config.UserContext, memory_id: str, op: str, fn: Callable[[Any], Any]) -> dict[str, Any]:
    """Shared path for update/delete/history -- see module docstring's
    point 2. Verifies ownership via a scoped get() before ever calling
    fn(memory), retries once with a freshly re-derived user_id on
    mismatch, fails closed (never touches the row) if that also doesn't
    check out."""
    async def _owns(uid: int) -> bool:
        existing = await _run_scoped(uid, lambda m: m.get(memory_id))
        owner = (existing or {}).get("user_id") if existing else None
        return existing is not None and str(owner) == str(uid)

    if await _owns(user.user_id):
        return await _run_scoped(user.user_id, fn)

    _log_incident(op, user.user_id, user.user_id, memory_id, "ownership check failed on first attempt")
    retried_uid = await _trusted_user_id(user)
    if retried_uid != user.user_id and await _owns(retried_uid):
        return await _run_scoped(retried_uid, fn)

    _log_incident(op, user.user_id, retried_uid, memory_id, "retry also failed -- failing closed")
    return {"error": f"Couldn't find memory {memory_id} for this user."}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def add_memory(user: config.UserContext, messages: list[dict[str, str]]) -> dict[str, Any]:
    """Writer -- called ONLY by run_daily_memory_batch below, never on the
    live per-turn hot path. One Mem0 add() call (Mem0's current pipeline
    does exactly one LLM call for extraction, not one per fact -- see
    README)."""
    if not _available():
        return {"error": NOT_CONFIGURED}
    return await _run_scoped(user.user_id, lambda m: m.add(messages, user_id=str(user.user_id)))


async def search_memory(user: config.UserContext, query: str, top_k: int = 5) -> dict[str, Any]:
    """The ONLY place a live texting turn ever calls into Mem0 -- backing
    agents/registry.py's recall_past_conversation tool. search() is one of
    the two Mem0 methods confirmed to require and enforce a user_id filter
    on its own, so no extra ownership guard is needed here beyond the RLS
    scoping _run_scoped already applies."""
    if not _available():
        return {"error": NOT_CONFIGURED, "results": []}
    return await _run_scoped(
        user.user_id, lambda m: m.search(query, user_id=str(user.user_id), top_k=top_k)
    )


async def get_all_memories(user: config.UserContext) -> dict[str, Any]:
    if not _available():
        return {"error": NOT_CONFIGURED, "results": []}
    return await _run_scoped(user.user_id, lambda m: m.get_all(filters={"user_id": str(user.user_id)}))


async def update_memory(user: config.UserContext, memory_id: str, text: str) -> dict[str, Any]:
    if not _available():
        return {"error": NOT_CONFIGURED}
    return await _guarded_mutation(user, memory_id, "update", lambda m: m.update(memory_id, text=text))


async def delete_memory(user: config.UserContext, memory_id: str) -> dict[str, Any]:
    if not _available():
        return {"error": NOT_CONFIGURED}
    return await _guarded_mutation(user, memory_id, "delete", lambda m: m.delete(memory_id))


async def get_memory_history(user: config.UserContext, memory_id: str) -> dict[str, Any]:
    if not _available():
        return {"error": NOT_CONFIGURED}
    return await _guarded_mutation(user, memory_id, "history", lambda m: {"history": m.history(memory_id)})


# ---------------------------------------------------------------------------
# Daily batch job -- see server.py's _production_memory_batch_loop for the
# once-a-day scheduling wrapper around this.
# ---------------------------------------------------------------------------

def _messages_to_mem0_format(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """message_history rows -> Mem0's own {"role", "content"} shape.
    message_history's `role` column is already 'user'/'assistant'/'system'
    (migrations' message_role ENUM) -- Mem0 expects exactly that
    vocabulary, so no translation needed beyond the key rename."""
    return [{"role": r["role"], "content": r["content"]} for r in rows if r.get("content")]


def _synthesize_profile_text(add_result: dict[str, Any], all_memories: dict[str, Any]) -> str | None:
    """Builds the short digest written to users.memory_profile from
    whatever Mem0 currently has on file for this user -- a plain joined
    list of memory texts (Mem0 already dedupes/merges facts itself, see
    README), capped so the digest stays cheap to inject into the system
    prompt every turn. Returns None (don't overwrite) if Mem0 has nothing
    yet, e.g. a brand new user's first day."""
    results = (all_memories or {}).get("results") or []
    facts = [r.get("memory") for r in results if r.get("memory")]
    if not facts:
        return None
    digest = "; ".join(facts)
    return digest[:1500]


async def run_daily_memory_batch(user: config.UserContext, since: datetime) -> dict[str, Any]:
    """One user's share of the daily batch: pulls that day's messages,
    calls add_memory() once (episodic + profile facts, in Mem0), then
    reads Mem0's current state back and writes a fresh digest into
    users.memory_profile for tomorrow's cheap hot-path reads. Returns a
    small status dict rather than raising, so server.py's loop can log a
    per-user failure and keep going for the rest of the batch."""
    if not _available():
        return {"status": "skipped", "reason": NOT_CONFIGURED}

    messages_rows = await db.get_messages_since(user.user_id, since)
    mem0_messages = _messages_to_mem0_format(messages_rows)
    if not mem0_messages:
        return {"status": "skipped", "reason": "no messages in window"}

    add_result = await add_memory(user, mem0_messages)
    if isinstance(add_result, dict) and add_result.get("error"):
        return {"status": "failed", "reason": add_result["error"]}

    all_memories = await get_all_memories(user)
    if isinstance(all_memories, dict) and all_memories.get("error"):
        return {"status": "partial", "reason": "add succeeded, profile refresh failed: " + str(all_memories["error"])}

    digest = _synthesize_profile_text(add_result if isinstance(add_result, dict) else {}, all_memories)
    if digest:
        await db.set_memory_profile(user.user_id, digest)
        return {"status": "ok", "digest_len": len(digest)}
    return {"status": "ok", "digest_len": 0}
