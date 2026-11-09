-- Keep canonical graph facts semantic; attach source and ACL scope through evidence mappings.

-- Composite candidate keys let evidence foreign keys enforce project/root isolation.
CREATE UNIQUE INDEX IF NOT EXISTS kb_relations_id_project_unique
    ON public.kb_relations (id, project_id);
CREATE UNIQUE INDEX IF NOT EXISTS project_knowledge_id_project_root_unique
    ON public.project_knowledge (id, project_id, root_id);
CREATE UNIQUE INDEX IF NOT EXISTS kb_documents_id_project_root_unique
    ON public.kb_documents (id, project_id, root_id);
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_provenance_id_project_unique
    ON public.knowledge_provenance (id, project_id);

CREATE TABLE IF NOT EXISTS public.entity_evidence (
    id bigserial PRIMARY KEY,
    entity_id bigint NOT NULL,
    project_id text NOT NULL,
    root_id text NOT NULL,
    document_id bigint,
    provenance_id bigint,
    chunk_id bigint,
    visibility text NOT NULL,
    agent_id text,
    session_id text,
    validation_status text NOT NULL DEFAULT 'unverified',
    is_active boolean NOT NULL DEFAULT true,
    source_ref text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT entity_evidence_entity_project_fk
        FOREIGN KEY (entity_id, project_id)
        REFERENCES public.kb_entities (id, project_id) ON DELETE CASCADE,
    CONSTRAINT entity_evidence_document_scope_fk
        FOREIGN KEY (document_id, project_id, root_id)
        REFERENCES public.kb_documents (id, project_id, root_id)
        ON DELETE SET NULL (document_id),
    CONSTRAINT entity_evidence_provenance_project_fk
        FOREIGN KEY (provenance_id, project_id)
        REFERENCES public.knowledge_provenance (id, project_id)
        ON DELETE SET NULL (provenance_id),
    CONSTRAINT entity_evidence_chunk_scope_fk
        FOREIGN KEY (chunk_id, project_id, root_id)
        REFERENCES public.project_knowledge (id, project_id, root_id)
        ON DELETE SET NULL (chunk_id),
    CONSTRAINT entity_evidence_project_root_check CHECK (
        btrim(project_id) <> '' AND btrim(root_id) <> ''
    ),
    CONSTRAINT entity_evidence_visibility_check CHECK (
        visibility IN ('public', 'shared', 'agent-private', 'session-private')
    ),
    CONSTRAINT entity_evidence_owner_check CHECK (
        (visibility IN ('public', 'shared') AND agent_id IS NULL AND session_id IS NULL)
        OR (
            visibility = 'agent-private'
            AND agent_id IS NOT NULL AND btrim(agent_id) <> ''
            AND session_id IS NULL
        )
        OR (
            visibility = 'session-private'
            AND agent_id IS NOT NULL AND btrim(agent_id) <> ''
            AND session_id IS NOT NULL AND btrim(session_id) <> ''
        )
    ),
    CONSTRAINT entity_evidence_validation_status_check CHECK (
        validation_status IN (
            'unverified', 'validated', 'authoritative', 'deprecated', 'invalidated'
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS entity_evidence_identity_unique
    ON public.entity_evidence (
        entity_id,
        project_id,
        root_id,
        visibility,
        COALESCE(agent_id, ''),
        COALESCE(session_id, ''),
        COALESCE(document_id, 0),
        COALESCE(provenance_id, 0),
        COALESCE(chunk_id, 0),
        COALESCE(source_ref, '')
    );
CREATE INDEX IF NOT EXISTS entity_evidence_scope_idx
    ON public.entity_evidence (
        project_id, root_id, visibility, agent_id, session_id, is_active, entity_id
    );
CREATE INDEX IF NOT EXISTS entity_evidence_source_idx
    ON public.entity_evidence (project_id, root_id, chunk_id, document_id, provenance_id);

CREATE TABLE IF NOT EXISTS public.relation_evidence (
    id bigserial PRIMARY KEY,
    relation_id bigint NOT NULL,
    project_id text NOT NULL,
    root_id text NOT NULL,
    document_id bigint,
    provenance_id bigint,
    chunk_id bigint,
    visibility text NOT NULL,
    agent_id text,
    session_id text,
    validation_status text NOT NULL DEFAULT 'unverified',
    is_active boolean NOT NULL DEFAULT true,
    source_ref text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT relation_evidence_relation_project_fk
        FOREIGN KEY (relation_id, project_id)
        REFERENCES public.kb_relations (id, project_id) ON DELETE CASCADE,
    CONSTRAINT relation_evidence_document_scope_fk
        FOREIGN KEY (document_id, project_id, root_id)
        REFERENCES public.kb_documents (id, project_id, root_id)
        ON DELETE SET NULL (document_id),
    CONSTRAINT relation_evidence_provenance_project_fk
        FOREIGN KEY (provenance_id, project_id)
        REFERENCES public.knowledge_provenance (id, project_id)
        ON DELETE SET NULL (provenance_id),
    CONSTRAINT relation_evidence_chunk_scope_fk
        FOREIGN KEY (chunk_id, project_id, root_id)
        REFERENCES public.project_knowledge (id, project_id, root_id)
        ON DELETE SET NULL (chunk_id),
    CONSTRAINT relation_evidence_project_root_check CHECK (
        btrim(project_id) <> '' AND btrim(root_id) <> ''
    ),
    CONSTRAINT relation_evidence_visibility_check CHECK (
        visibility IN ('public', 'shared', 'agent-private', 'session-private')
    ),
    CONSTRAINT relation_evidence_owner_check CHECK (
        (visibility IN ('public', 'shared') AND agent_id IS NULL AND session_id IS NULL)
        OR (
            visibility = 'agent-private'
            AND agent_id IS NOT NULL AND btrim(agent_id) <> ''
            AND session_id IS NULL
        )
        OR (
            visibility = 'session-private'
            AND agent_id IS NOT NULL AND btrim(agent_id) <> ''
            AND session_id IS NOT NULL AND btrim(session_id) <> ''
        )
    ),
    CONSTRAINT relation_evidence_validation_status_check CHECK (
        validation_status IN (
            'unverified', 'validated', 'authoritative', 'deprecated', 'invalidated'
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS relation_evidence_identity_unique
    ON public.relation_evidence (
        relation_id,
        project_id,
        root_id,
        visibility,
        COALESCE(agent_id, ''),
        COALESCE(session_id, ''),
        COALESCE(document_id, 0),
        COALESCE(provenance_id, 0),
        COALESCE(chunk_id, 0),
        COALESCE(source_ref, '')
    );
CREATE INDEX IF NOT EXISTS relation_evidence_scope_idx
    ON public.relation_evidence (
        project_id, root_id, visibility, agent_id, session_id, is_active, relation_id
    );
CREATE INDEX IF NOT EXISTS relation_evidence_source_idx
    ON public.relation_evidence (project_id, root_id, chunk_id, document_id, provenance_id);
