-- Migration 027: two-tier memory (Mem0 + Postgres/pgvector), with Row-Level
-- Security as a real, database-enforced safety backstop -- not just app-
-- level user_id filtering.
--
-- Purely additive, safe to re-run, safe to leave unapplied for a while
-- (same "the app works without this yet, db.py checks first" shape as
-- every other migration here): the profile digest columns below are read
-- through _has_column-guarded helpers, and messa/memory.py's whole memory
-- feature no-ops cleanly (returns "memory not configured yet" style
-- results instead of crashing) whenever MESSA_MEM0_DIRECT_DATABASE_URL
-- isn't set -- see .env.example and README's "Memory" section for why that
-- var has to be Neon's DIRECT/unpooled connection string, not the app's
-- existing pooled DATABASE_URL.
--
-- 1. Profile digest -- a small, cheap, always-on summary of the user Messa
--    quietly personalizes from, read once per turn via the app's existing
--    asyncpg pool (see db.get_memory_profile) exactly like default_email_
--    provider/city/etc. already are. Nothing here ever calls Mem0 on that
--    hot path -- Mem0 only gets touched by the daily batch job that writes
--    this column, and by the on-demand recall_past_conversation tool.
ALTER TABLE users ADD COLUMN IF NOT EXISTS memory_profile TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS memory_profile_updated_at TIMESTAMPTZ;

-- 2. Mem0's own episodic/semantic store. Mem0's pgvector backend normally
--    auto-creates this table itself on first use (id uuid, vector, jsonb
--    payload, its own HNSW + lemmatized-text GIN index) -- it's created
--    explicitly here instead, for the same shape, so the RLS policy below
--    can be reviewed and applied in the same migration rather than bolted
--    on after Mem0 auto-creates it. embedding dimension is 1536 to match
--    the existing (unused) vector_memories table's own assumption below;
--    if the real embedder ends up being lower-dimensional (e.g. a local
--    fastembed model), this column's dimension needs a follow-up
--    migration to match -- see README's "Memory" section, this is the one
--    open item flagged there.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS mem0_memories (
    id UUID PRIMARY KEY,
    vector vector(1536),
    payload JSONB
);
CREATE INDEX IF NOT EXISTS mem0_memories_hnsw_idx
    ON mem0_memories USING hnsw (vector vector_cosine_ops);
CREATE INDEX IF NOT EXISTS mem0_memories_text_lemmatized_idx
    ON mem0_memories USING gin (to_tsvector('simple', payload ->> 'text_lemmatized'));

-- 3. The backstop. messa/memory.py always sets a per-call, per-user
--    Postgres session variable (app.current_user_id) on a dedicated,
--    single-connection, non-pooled (direct, not PgBouncer) connection
--    before ever touching this table -- see messa/memory.py and README's
--    "Memory" section for exactly why it has to be a direct connection,
--    not the app's normal pooled one, for this SET to reliably still be
--    in effect for the query that follows it.
--
--    This is deliberately a hard backstop, not just defense-in-depth
--    against a THEORETICAL bug: Mem0's own update()/delete()/history()
--    methods take a bare memory_id with no user_id parameter and NO
--    built-in ownership check at all (confirmed directly against Mem0's
--    source, not assumed) -- so without this policy, a wrong memory_id
--    anywhere in our own code (e.g. a mixed-up variable under concurrent
--    requests) would silently let one user's request touch another user's
--    memory. With this policy, Postgres itself refuses that query outright
--    regardless of what our application code does.
ALTER TABLE mem0_memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE mem0_memories FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS mem0_memories_user_isolation ON mem0_memories;
CREATE POLICY mem0_memories_user_isolation ON mem0_memories
    USING (payload ->> 'user_id' = current_setting('app.current_user_id', true))
    WITH CHECK (payload ->> 'user_id' = current_setting('app.current_user_id', true));

-- Note: this policy only actually blocks anything if the connecting
-- Postgres role does NOT have BYPASSRLS/superuser (table owners and
-- superusers bypass RLS by default regardless of policies). Verify this
-- with:
--   SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user;
-- Neon's default non-superuser app roles don't have BYPASSRLS, but this is
-- worth confirming once against your own real DATABASE_URL/MESSA_MEM0_
-- DIRECT_DATABASE_URL role before relying on this in production -- see
-- README's "Memory" section, "Before you rely on this" checklist.

-- 4. One tiny marker table so the daily memory-digest batch job
--    (messa/memory.py's run_daily_memory_batch, started from
--    server.py's _production_memory_batch_loop) runs at most once per UTC
--    calendar day, even across app restarts or (in principle) more than
--    one running instance -- INSERT ... ON CONFLICT DO NOTHING on
--    run_date is the whole mechanism, no separate locking needed. This is
--    a simpler, purpose-built alternative to threading a new per-user
--    cron_jobs row through the existing timezone-aware briefing machinery
--    (db.ensure_default_briefings/_compute_next_run_local): the digest job
--    has no user-facing delivery time to respect (unlike a 7am briefing),
--    it just needs to run once a day for every user, so it gets its own
--    minimal scheduling rather than borrowing timezone-per-user logic that
--    doesn't apply here.
CREATE TABLE IF NOT EXISTS memory_batch_runs (
    run_date DATE PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    users_processed INT NOT NULL DEFAULT 0,
    users_failed INT NOT NULL DEFAULT 0
);

-- 5. Cleanup: the old vector_memories table (neon-schema.sql) was built
--    ahead of an earlier plan for this same feature and was never wired
--    into any tool or query -- confirmed zero rows and zero references
--    anywhere in messa/ before dropping it here. Mem0's own mem0_memories
--    table above replaces it; nothing reads vector_memories today so this
--    is safe to drop rather than migrate.
DROP TABLE IF EXISTS vector_memories;
