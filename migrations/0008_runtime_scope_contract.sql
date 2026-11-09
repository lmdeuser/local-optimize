-- Enforce owner identity for every private graph row used by scoped traversal.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.kb_entities
        WHERE (visibility IN ('agent-private', 'session-private') AND agent_id IS NULL)
           OR (visibility = 'session-private' AND session_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'Private kb_entities rows require explicit owner identity';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.kb_relations
        WHERE (visibility IN ('agent-private', 'session-private') AND agent_id IS NULL)
           OR (visibility = 'session-private' AND session_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'Private kb_relations rows require explicit owner identity';
    END IF;
END $$;

ALTER TABLE public.kb_entities
    DROP CONSTRAINT IF EXISTS kb_entities_private_owner_check;
ALTER TABLE public.kb_entities
    ADD CONSTRAINT kb_entities_private_owner_check CHECK (
        (visibility NOT IN ('agent-private', 'session-private') OR agent_id IS NOT NULL)
        AND (visibility <> 'session-private' OR session_id IS NOT NULL)
    );

ALTER TABLE public.kb_relations
    DROP CONSTRAINT IF EXISTS kb_relations_private_owner_check;
ALTER TABLE public.kb_relations
    ADD CONSTRAINT kb_relations_private_owner_check CHECK (
        (visibility NOT IN ('agent-private', 'session-private') OR agent_id IS NOT NULL)
        AND (visibility <> 'session-private' OR session_id IS NOT NULL)
    );
