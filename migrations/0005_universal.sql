-- Migration v5: Universal Multi-Agent RAG Infrastructure Schema Upgrade
-- Idempotent upgrade script for PostgreSQL 18 with pgvector

-- 1. Create kb_documents table if not exists
CREATE TABLE IF NOT EXISTS kb_documents (
    id           bigserial    PRIMARY KEY,
    source_uri   text         NOT NULL,
    source_type  text         NOT NULL DEFAULT 'file',
    title        text,
    content_hash text         NOT NULL,
    language     text         DEFAULT 'en',
    version      text         DEFAULT '1.0',
    is_deleted   boolean      DEFAULT false,
    metadata     jsonb        DEFAULT '{}'::jsonb,
    created_at   timestamptz  DEFAULT now(),
    updated_at   timestamptz  DEFAULT now(),
    indexed_at   timestamptz  DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kb_docs_source_uri_idx ON kb_documents(source_uri);
CREATE INDEX IF NOT EXISTS kb_docs_content_hash_idx ON kb_documents(content_hash);

-- 2. Upgrade project_knowledge table
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='project_knowledge' AND column_name='document_id') THEN
        ALTER TABLE project_knowledge ADD COLUMN document_id bigint REFERENCES kb_documents(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='project_knowledge' AND column_name='source_uri') THEN
        ALTER TABLE project_knowledge ADD COLUMN source_uri text;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='project_knowledge' AND column_name='source_type') THEN
        ALTER TABLE project_knowledge ADD COLUMN source_type text DEFAULT 'code';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='project_knowledge' AND column_name='chunk_index') THEN
        ALTER TABLE project_knowledge ADD COLUMN chunk_index integer DEFAULT 0;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='project_knowledge' AND column_name='start_locator') THEN
        ALTER TABLE project_knowledge ADD COLUMN start_locator text;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='project_knowledge' AND column_name='end_locator') THEN
        ALTER TABLE project_knowledge ADD COLUMN end_locator text;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='project_knowledge' AND column_name='token_count') THEN
        ALTER TABLE project_knowledge ADD COLUMN token_count integer DEFAULT 0;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='project_knowledge' AND column_name='embedding_model') THEN
        ALTER TABLE project_knowledge ADD COLUMN embedding_model text DEFAULT 'all-MiniLM-L6-v2';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='project_knowledge' AND column_name='embedding_version') THEN
        ALTER TABLE project_knowledge ADD COLUMN embedding_version text DEFAULT '1.0';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='project_knowledge' AND column_name='embedding_dimension') THEN
        ALTER TABLE project_knowledge ADD COLUMN embedding_dimension integer DEFAULT 384;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='project_knowledge' AND column_name='validation_status') THEN
        ALTER TABLE project_knowledge ADD COLUMN validation_status text DEFAULT 'unverified';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='project_knowledge' AND column_name='valid_from') THEN
        ALTER TABLE project_knowledge ADD COLUMN valid_from timestamptz DEFAULT now();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='project_knowledge' AND column_name='valid_to') THEN
        ALTER TABLE project_knowledge ADD COLUMN valid_to timestamptz;
    END IF;
END $$;

-- Validation status check constraint
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'project_knowledge_validation_status_check') THEN
        ALTER TABLE project_knowledge ADD CONSTRAINT project_knowledge_validation_status_check
        CHECK (validation_status IN ('unverified', 'validated', 'authoritative', 'deprecated', 'invalidated'));
    END IF;
END $$;

-- Indexes for project_knowledge
CREATE INDEX IF NOT EXISTS pk_validation_status_idx ON project_knowledge(validation_status);
CREATE INDEX IF NOT EXISTS pk_content_hash_idx ON project_knowledge(content_hash);
CREATE INDEX IF NOT EXISTS pk_fts_tokens_idx ON project_knowledge USING gin(fts_tokens);

-- 3. Upgrade kb_entities
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='kb_entities' AND column_name='canonical_name') THEN
        ALTER TABLE kb_entities ADD COLUMN canonical_name text;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='kb_entities' AND column_name='aliases') THEN
        ALTER TABLE kb_entities ADD COLUMN aliases text[] DEFAULT '{}';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='kb_entities' AND column_name='scope') THEN
        ALTER TABLE kb_entities ADD COLUMN scope text DEFAULT 'global';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='kb_entities' AND column_name='confidence') THEN
        ALTER TABLE kb_entities ADD COLUMN confidence double precision DEFAULT 1.0;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='kb_entities' AND column_name='source_chunk_id') THEN
        ALTER TABLE kb_entities ADD COLUMN source_chunk_id bigint REFERENCES project_knowledge(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='kb_entities' AND column_name='updated_at') THEN
        ALTER TABLE kb_entities ADD COLUMN updated_at timestamptz DEFAULT now();
    END IF;
END $$;

-- 4. Upgrade kb_relations
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='kb_relations' AND column_name='direction') THEN
        ALTER TABLE kb_relations ADD COLUMN direction text DEFAULT 'directed';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='kb_relations' AND column_name='evidence_chunk_id') THEN
        ALTER TABLE kb_relations ADD COLUMN evidence_chunk_id bigint REFERENCES project_knowledge(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='kb_relations' AND column_name='valid_from') THEN
        ALTER TABLE kb_relations ADD COLUMN valid_from timestamptz DEFAULT now();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='kb_relations' AND column_name='valid_to') THEN
        ALTER TABLE kb_relations ADD COLUMN valid_to timestamptz;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='kb_relations' AND column_name='status') THEN
        ALTER TABLE kb_relations ADD COLUMN status text DEFAULT 'active';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'kb_relations_status_check') THEN
        ALTER TABLE kb_relations ADD CONSTRAINT kb_relations_status_check
        CHECK (status IN ('active', 'deprecated', 'invalidated'));
    END IF;
END $$;

-- 5. Upgrade agent_messages for Queue concurrency
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='agent_messages' AND column_name='status') THEN
        ALTER TABLE agent_messages ADD COLUMN status text DEFAULT 'pending';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='agent_messages' AND column_name='priority') THEN
        ALTER TABLE agent_messages ADD COLUMN priority integer DEFAULT 0;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='agent_messages' AND column_name='idempotency_key') THEN
        ALTER TABLE agent_messages ADD COLUMN idempotency_key text;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='agent_messages' AND column_name='attempt_count') THEN
        ALTER TABLE agent_messages ADD COLUMN attempt_count integer DEFAULT 0;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='agent_messages' AND column_name='available_at') THEN
        ALTER TABLE agent_messages ADD COLUMN available_at timestamptz DEFAULT now();
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='agent_messages' AND column_name='locked_at') THEN
        ALTER TABLE agent_messages ADD COLUMN locked_at timestamptz;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='agent_messages' AND column_name='locked_by') THEN
        ALTER TABLE agent_messages ADD COLUMN locked_by text;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='agent_messages' AND column_name='processed_at') THEN
        ALTER TABLE agent_messages ADD COLUMN processed_at timestamptz;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='agent_messages' AND column_name='last_error') THEN
        ALTER TABLE agent_messages ADD COLUMN last_error text;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agent_messages_status_check') THEN
        ALTER TABLE agent_messages ADD CONSTRAINT agent_messages_status_check
        CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'dead_letter', 'cancelled'));
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS agent_messages_idempotency_idx ON agent_messages(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS agent_messages_queue_idx ON agent_messages(to_agent, status, priority DESC, available_at) WHERE status IN ('pending', 'processing');


