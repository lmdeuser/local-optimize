-- Project/security scope, provenance-preserving deduplication, and graph isolation.

ALTER TABLE public.project_knowledge ADD COLUMN IF NOT EXISTS project_id text;
ALTER TABLE public.project_knowledge ADD COLUMN IF NOT EXISTS root_id text;
ALTER TABLE public.project_knowledge ADD COLUMN IF NOT EXISTS project_root text;
ALTER TABLE public.project_knowledge ADD COLUMN IF NOT EXISTS dedup_key text;

UPDATE public.project_knowledge
SET project_name = COALESCE(NULLIF(project_name, ''), 'legacy-project'),
    project_id = COALESCE(NULLIF(project_id, ''), NULLIF(project_name, ''), 'legacy-project'),
    root_id = COALESCE(NULLIF(root_id, ''), 'legacy-root'),
    project_root = COALESCE(project_root, ''),
    file_path = COALESCE(file_path, ''),
    source_uri = COALESCE(source_uri, ''),
    source_type = COALESCE(source_type, 'code'),
    visibility = COALESCE(visibility, 'shared'),
    validation_status = COALESCE(validation_status, 'unverified'),
    dedup_key = COALESCE(NULLIF(dedup_key, ''), md5(concat_ws(E'\x1f',
        COALESCE(NULLIF(project_id, ''), NULLIF(project_name, ''), 'legacy-project'),
        content_hash,
        COALESCE(visibility, 'shared'),
        COALESCE(agent_id, ''),
        COALESCE(session_id, ''),
        COALESCE(validation_status, 'unverified'))));

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.project_knowledge
        WHERE visibility = 'private' AND (agent_id IS NULL OR session_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'Legacy private rows require explicit agent_id and session_id reconciliation';
    END IF;
END $$;

UPDATE public.project_knowledge
SET visibility = 'session-private'
WHERE visibility = 'private';

ALTER TABLE public.project_knowledge DROP CONSTRAINT IF EXISTS project_knowledge_visibility_check;
ALTER TABLE public.project_knowledge DROP CONSTRAINT IF EXISTS project_knowledge_validation_status_check;
ALTER TABLE public.project_knowledge
    ADD CONSTRAINT project_knowledge_visibility_check
    CHECK (visibility IN ('public', 'shared', 'agent-private', 'session-private'));
ALTER TABLE public.project_knowledge
    ADD CONSTRAINT project_knowledge_validation_status_check
    CHECK (validation_status IN ('unverified', 'validated', 'authoritative', 'deprecated', 'invalidated'));
ALTER TABLE public.project_knowledge
    ADD CONSTRAINT project_knowledge_private_owner_check CHECK (
        (visibility NOT IN ('agent-private', 'session-private') OR agent_id IS NOT NULL)
        AND (visibility <> 'session-private' OR session_id IS NOT NULL)
    );

ALTER TABLE public.project_knowledge ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE public.project_knowledge ALTER COLUMN project_name SET NOT NULL;
ALTER TABLE public.project_knowledge ALTER COLUMN root_id SET NOT NULL;
ALTER TABLE public.project_knowledge ALTER COLUMN project_root SET NOT NULL;
ALTER TABLE public.project_knowledge ALTER COLUMN file_path SET NOT NULL;
ALTER TABLE public.project_knowledge ALTER COLUMN source_uri SET NOT NULL;
ALTER TABLE public.project_knowledge ALTER COLUMN source_type SET NOT NULL;
ALTER TABLE public.project_knowledge ALTER COLUMN visibility SET NOT NULL;
ALTER TABLE public.project_knowledge ALTER COLUMN validation_status SET NOT NULL;
ALTER TABLE public.project_knowledge ALTER COLUMN dedup_key SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS project_knowledge_dedup_key_unique
    ON public.project_knowledge (dedup_key);
CREATE INDEX IF NOT EXISTS project_knowledge_project_scope_idx
    ON public.project_knowledge (project_id, visibility, agent_id, session_id);

ALTER TABLE public.kb_documents ADD COLUMN IF NOT EXISTS project_id text;
ALTER TABLE public.kb_documents ADD COLUMN IF NOT EXISTS project_name text;
ALTER TABLE public.kb_documents ADD COLUMN IF NOT EXISTS root_id text;
ALTER TABLE public.kb_documents ADD COLUMN IF NOT EXISTS project_root text;
ALTER TABLE public.kb_documents ADD COLUMN IF NOT EXISTS file_path text;
UPDATE public.kb_documents
SET project_id = COALESCE(NULLIF(project_id, ''), 'legacy-project'),
    project_name = COALESCE(NULLIF(project_name, ''), 'legacy-project'),
    root_id = COALESCE(NULLIF(root_id, ''), 'legacy-root'),
    project_root = COALESCE(project_root, ''),
    file_path = COALESCE(file_path, '');
ALTER TABLE public.kb_documents ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE public.kb_documents ALTER COLUMN project_name SET NOT NULL;
ALTER TABLE public.kb_documents ALTER COLUMN root_id SET NOT NULL;
ALTER TABLE public.kb_documents ALTER COLUMN project_root SET NOT NULL;
ALTER TABLE public.kb_documents ALTER COLUMN file_path SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS kb_documents_project_source_unique
    ON public.kb_documents (project_id, source_uri, file_path);

CREATE TABLE IF NOT EXISTS public.knowledge_provenance (
    id bigserial PRIMARY KEY,
    knowledge_id bigint NOT NULL REFERENCES public.project_knowledge(id) ON DELETE CASCADE,
    document_id bigint REFERENCES public.kb_documents(id) ON DELETE SET NULL,
    project_id text NOT NULL,
    source_uri text NOT NULL DEFAULT '',
    source_type text NOT NULL,
    file_path text NOT NULL DEFAULT '',
    start_locator text NOT NULL DEFAULT '',
    end_locator text NOT NULL DEFAULT '',
    reference_key text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (knowledge_id, reference_key)
);
CREATE INDEX IF NOT EXISTS knowledge_provenance_project_idx
    ON public.knowledge_provenance (project_id, file_path);

ALTER TABLE public.kb_entities ADD COLUMN IF NOT EXISTS project_id text;
ALTER TABLE public.kb_entities ADD COLUMN IF NOT EXISTS visibility text;
ALTER TABLE public.kb_entities ADD COLUMN IF NOT EXISTS agent_id text;
ALTER TABLE public.kb_entities ADD COLUMN IF NOT EXISTS session_id text;
UPDATE public.kb_entities e
SET project_id = COALESCE(NULLIF(e.project_id, ''), NULLIF(e.metadata->>'project_id', ''), 'legacy-project'),
    visibility = COALESCE(e.visibility, 'shared');
ALTER TABLE public.kb_entities ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE public.kb_entities ALTER COLUMN visibility SET NOT NULL;
ALTER TABLE public.kb_entities
    ADD CONSTRAINT kb_entities_visibility_check
    CHECK (visibility IN ('public', 'shared', 'agent-private', 'session-private'));
CREATE UNIQUE INDEX IF NOT EXISTS kb_entities_project_stable_unique
    ON public.kb_entities (project_id, stable_key);
CREATE UNIQUE INDEX IF NOT EXISTS kb_entities_id_project_unique
    ON public.kb_entities (id, project_id);

ALTER TABLE public.kb_relations ADD COLUMN IF NOT EXISTS project_id text;
ALTER TABLE public.kb_relations ADD COLUMN IF NOT EXISTS visibility text;
ALTER TABLE public.kb_relations ADD COLUMN IF NOT EXISTS agent_id text;
ALTER TABLE public.kb_relations ADD COLUMN IF NOT EXISTS session_id text;
UPDATE public.kb_relations r
SET project_id = e.project_id,
    visibility = COALESCE(r.visibility, e.visibility, 'shared'),
    agent_id = COALESCE(r.agent_id, e.agent_id),
    session_id = COALESCE(r.session_id, e.session_id)
FROM public.kb_entities e
WHERE e.id = r.from_id;
ALTER TABLE public.kb_relations ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE public.kb_relations ALTER COLUMN visibility SET NOT NULL;
ALTER TABLE public.kb_relations
    ADD CONSTRAINT kb_relations_visibility_check
    CHECK (visibility IN ('public', 'shared', 'agent-private', 'session-private'));
ALTER TABLE public.kb_relations
    ADD CONSTRAINT kb_relations_from_project_fk
    FOREIGN KEY (from_id, project_id) REFERENCES public.kb_entities(id, project_id) ON DELETE CASCADE;
ALTER TABLE public.kb_relations
    ADD CONSTRAINT kb_relations_to_project_fk
    FOREIGN KEY (to_id, project_id) REFERENCES public.kb_entities(id, project_id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS kb_relations_security_scope_idx
    ON public.kb_relations (project_id, visibility, agent_id, session_id, from_id, to_id);
CREATE INDEX IF NOT EXISTS kb_relations_from_idx ON public.kb_relations(from_id);
CREATE INDEX IF NOT EXISTS kb_relations_to_idx ON public.kb_relations(to_id);
CREATE INDEX IF NOT EXISTS kb_relations_type_idx ON public.kb_relations(relation_type);
CREATE INDEX IF NOT EXISTS kb_entities_type_idx ON public.kb_entities(type);
CREATE INDEX IF NOT EXISTS kb_entities_name_lower_idx ON public.kb_entities(lower(name));
CREATE INDEX IF NOT EXISTS kb_entities_metadata_idx ON public.kb_entities USING GIN(metadata);

CREATE TABLE IF NOT EXISTS public.kb_feedback_logs (
    id bigserial PRIMARY KEY,
    project_id text NOT NULL,
    agent_id text,
    session_id text,
    query text NOT NULL,
    feedback_type text NOT NULL,
    user_correction text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS kb_feedback_logs_project_idx
    ON public.kb_feedback_logs(project_id, created_at DESC);
