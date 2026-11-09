CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version text PRIMARY KEY,
    filename text NOT NULL,
    checksum text NOT NULL,
    execution_status text NOT NULL DEFAULT 'applied',
    applied_at timestamptz NOT NULL DEFAULT now(),
    reconciled_at timestamptz,
    reconciliation_method text,
    CHECK (execution_status IN ('applied', 'legacy-unverified'))
);

ALTER TABLE public.schema_migrations ADD COLUMN IF NOT EXISTS filename text NOT NULL DEFAULT '';
ALTER TABLE public.schema_migrations ADD COLUMN IF NOT EXISTS checksum text NOT NULL DEFAULT '';
ALTER TABLE public.schema_migrations ADD COLUMN IF NOT EXISTS execution_status text NOT NULL DEFAULT 'applied';
ALTER TABLE public.schema_migrations ADD COLUMN IF NOT EXISTS reconciled_at timestamptz;
ALTER TABLE public.schema_migrations ADD COLUMN IF NOT EXISTS reconciliation_method text;

CREATE TABLE IF NOT EXISTS public.project_knowledge (
    id bigserial PRIMARY KEY,
    project_name text,
    file_path text,
    content text NOT NULL,
    content_hash text NOT NULL,
    embedding vector(384),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    entity_id bigint,
    fts_tokens tsvector,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.kb_entities (
    id bigserial PRIMARY KEY,
    name text NOT NULL,
    type text NOT NULL,
    description text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.kb_relations (
    id bigserial PRIMARY KEY,
    from_id bigint NOT NULL REFERENCES public.kb_entities(id) ON DELETE CASCADE,
    to_id bigint NOT NULL REFERENCES public.kb_entities(id) ON DELETE CASCADE,
    relation_type text NOT NULL DEFAULT 'relates_to',
    weight double precision NOT NULL DEFAULT 1.0,
    confidence double precision NOT NULL DEFAULT 1.0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (from_id, to_id, relation_type)
);
