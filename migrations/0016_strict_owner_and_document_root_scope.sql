-- Enforce non-blank private owner identifiers and root-scope kb_documents uniqueness.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.project_knowledge
        WHERE (visibility IN ('agent-private', 'session-private')
               AND (agent_id IS NULL OR btrim(agent_id) = ''))
           OR (visibility = 'session-private'
               AND (session_id IS NULL OR btrim(session_id) = ''))
    ) THEN
        RAISE EXCEPTION 'Private project_knowledge rows require non-blank owner identity';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.kb_entities
        WHERE (visibility IN ('agent-private', 'session-private')
               AND (agent_id IS NULL OR btrim(agent_id) = ''))
           OR (visibility = 'session-private'
               AND (session_id IS NULL OR btrim(session_id) = ''))
    ) THEN
        RAISE EXCEPTION 'Private kb_entities rows require non-blank owner identity';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.kb_relations
        WHERE (visibility IN ('agent-private', 'session-private')
               AND (agent_id IS NULL OR btrim(agent_id) = ''))
           OR (visibility = 'session-private'
               AND (session_id IS NULL OR btrim(session_id) = ''))
    ) THEN
        RAISE EXCEPTION 'Private kb_relations rows require non-blank owner identity';
    END IF;
END $$;

ALTER TABLE public.project_knowledge
    DROP CONSTRAINT IF EXISTS project_knowledge_owner_nonblank_check;
ALTER TABLE public.project_knowledge
    ADD CONSTRAINT project_knowledge_owner_nonblank_check CHECK (
        (visibility NOT IN ('agent-private', 'session-private')
         OR (agent_id IS NOT NULL AND btrim(agent_id) <> ''))
        AND (visibility <> 'session-private'
         OR (session_id IS NOT NULL AND btrim(session_id) <> ''))
    );

ALTER TABLE public.kb_entities
    DROP CONSTRAINT IF EXISTS kb_entities_owner_nonblank_check;
ALTER TABLE public.kb_entities
    ADD CONSTRAINT kb_entities_owner_nonblank_check CHECK (
        (visibility NOT IN ('agent-private', 'session-private')
         OR (agent_id IS NOT NULL AND btrim(agent_id) <> ''))
        AND (visibility <> 'session-private'
         OR (session_id IS NOT NULL AND btrim(session_id) <> ''))
    );

ALTER TABLE public.kb_relations
    DROP CONSTRAINT IF EXISTS kb_relations_owner_nonblank_check;
ALTER TABLE public.kb_relations
    ADD CONSTRAINT kb_relations_owner_nonblank_check CHECK (
        (visibility NOT IN ('agent-private', 'session-private')
         OR (agent_id IS NOT NULL AND btrim(agent_id) <> ''))
        AND (visibility <> 'session-private'
         OR (session_id IS NOT NULL AND btrim(session_id) <> ''))
    );

DROP INDEX IF EXISTS public.kb_documents_project_source_unique;
CREATE UNIQUE INDEX IF NOT EXISTS kb_documents_project_root_source_unique
    ON public.kb_documents (project_id, root_id, source_uri, file_path);

CREATE INDEX IF NOT EXISTS project_knowledge_project_root_file_idx
    ON public.project_knowledge (project_id, root_id, file_path);
