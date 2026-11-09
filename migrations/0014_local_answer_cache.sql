-- Store only validated local model answers for exact generation inputs.
--
-- `scope_hash` binds rows to the authorized ProjectContext. `prompt_hash`
-- covers the fully rendered prompt, model name, generation options and local
-- answer-cache version. The generation counter retires entries after any
-- successful knowledge mutation; TTL and LRU bounds keep storage finite.
-- Neither the prompt nor retrieved evidence is copied into this table.

CREATE TABLE IF NOT EXISTS public.rag_local_answer_cache (
    id              bigserial PRIMARY KEY,
    project_id      text        NOT NULL,
    scope_hash      text        NOT NULL,
    prompt_hash     text        NOT NULL,
    generation      bigint      NOT NULL,
    answer          text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    last_hit_at     timestamptz NOT NULL DEFAULT now(),
    hit_count       integer     NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS rag_local_answer_cache_key_idx
    ON public.rag_local_answer_cache (scope_hash, prompt_hash);

CREATE INDEX IF NOT EXISTS rag_local_answer_cache_expiry_idx
    ON public.rag_local_answer_cache (expires_at);

CREATE INDEX IF NOT EXISTS rag_local_answer_cache_project_idx
    ON public.rag_local_answer_cache (project_id, generation);

CREATE INDEX IF NOT EXISTS rag_local_answer_cache_lru_idx
    ON public.rag_local_answer_cache (last_hit_at);
