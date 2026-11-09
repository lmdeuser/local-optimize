-- Persist the search cache, so that a restart is not amnesia.
--
-- The engine already keeps a bounded TTL/LRU cache in process memory. That
-- cache is correct but it dies with the process, and it is invisible to every
-- other process: the MCP server, the CLI, the watchdog indexer and a second
-- worker each pay full price for the same question. Retrieval is the hot path
-- of an agent session, and the same handful of questions get asked over and
-- over across sessions, so the miss rate that matters is the one measured
-- across restarts, not within one.
--
-- Two tables, because invalidation and storage are different problems:
--
--   rag_cache_generation  one counter per project, bumped by any successful
--                         knowledge mutation. A cached row is only valid for
--                         the generation it was computed under, so a save from
--                         ANY process retires every other process's cached
--                         answers without needing to reach into their memory.
--                         Deleting rows would be the obvious alternative and it
--                         is worse: it races with in-flight readers and it
--                         cannot invalidate an entry a peer is holding in RAM.
--
--   rag_query_cache       the responses themselves, keyed by (scope_hash,
--                         query_hash).
--
-- `scope_hash` is what keeps this from becoming a data leak. It covers the
-- ENTIRE authorized context - project, root, read scope, owning agent and
-- session, source types, language, validation status, min score, registry
-- fingerprint - plus the embedding model provenance and every ranking
-- parameter. Two callers with different visibility produce different hashes and
-- therefore can never see each other's rows. Anything that changes what a
-- search is allowed to return, or how it ranks, must be inside that hash; if it
-- is not, this table serves one caller's rows to another.
--
-- `query_embedding` exists for near-miss reuse: agents rarely repeat a question
-- verbatim, but they paraphrase it constantly ("как кешируются запросы" vs
-- "кеширование запросов в раг"). A cosine neighbour above a deliberately high
-- threshold is the same question in different words, and answering it from
-- cache is the difference between a cache that hits ~10% of the time and one
-- that hits most of the time. The threshold is enforced in the engine, not
-- here, because it is a retrieval-quality decision and belongs next to the
-- other calibrated cuts.
--
-- Deliberately absent: any foreign key to project_knowledge. A cached response
-- is a denormalized snapshot, not a view; cascading deletes from the knowledge
-- store into the cache would turn every save into a write amplification, and
-- the generation counter already retires stale snapshots wholesale.

CREATE TABLE IF NOT EXISTS public.rag_cache_generation (
    project_id text PRIMARY KEY,
    generation  bigint      NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.rag_query_cache (
    id              bigserial PRIMARY KEY,
    project_id      text        NOT NULL,
    scope_hash      text        NOT NULL,
    query_hash      text        NOT NULL,
    query_text      text        NOT NULL,
    query_embedding vector(384),
    generation      bigint      NOT NULL,
    response        jsonb       NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    last_hit_at     timestamptz NOT NULL DEFAULT now(),
    hit_count       integer     NOT NULL DEFAULT 0
);

-- The exact-match path. Unique so a repeated question updates one row instead
-- of accumulating a row per asking.
CREATE UNIQUE INDEX IF NOT EXISTS rag_query_cache_key_idx
    ON public.rag_query_cache (scope_hash, query_hash);

-- Expiry sweeping and LRU trimming both scan by time; without these the
-- housekeeping that keeps the table small becomes the reason it is slow.
CREATE INDEX IF NOT EXISTS rag_query_cache_expiry_idx
    ON public.rag_query_cache (expires_at);

CREATE INDEX IF NOT EXISTS rag_query_cache_lru_idx
    ON public.rag_query_cache (last_hit_at);

-- Generation bumps retire rows in bulk per project.
CREATE INDEX IF NOT EXISTS rag_query_cache_project_idx
    ON public.rag_query_cache (project_id, generation);

-- Near-miss reuse. Cosine, to match the operator the retrieval path already
-- uses, so a similarity computed here means the same thing as one computed
-- against project_knowledge.
CREATE INDEX IF NOT EXISTS rag_query_cache_semantic_idx
    ON public.rag_query_cache
    USING hnsw (query_embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
