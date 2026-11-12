-- Scope graph identity by source root and owner without deleting existing facts.
-- Anchor rows keep their ids; additional roots receive independent entities and relations.

CREATE FUNCTION pg_temp.root_scoped_key(entity_row public.kb_entities, root_id text)
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $function$
    SELECT entity_row.project_id || ':' || root_id || ':' || entity_row.visibility || ':' ||
           COALESCE(entity_row.agent_id, '') || ':' || COALESCE(entity_row.session_id, '') || ':' ||
           COALESCE(
               entity_row.metadata->>'relative_path',
               entity_row.metadata->>'file',
               'legacy/' || entity_row.id::text
           ) || ':' || entity_row.type || ':' || entity_row.name
$function$;

ALTER TABLE public.knowledge_provenance
    ADD COLUMN IF NOT EXISTS root_id text;

UPDATE public.knowledge_provenance p
SET root_id = COALESCE(
    (SELECT k.root_id FROM public.project_knowledge k WHERE k.id = p.knowledge_id),
    (SELECT kd.root_id FROM public.kb_documents kd
     WHERE kd.id = p.document_id AND kd.project_id = p.project_id),
    'legacy'
)
WHERE p.root_id IS NULL;

ALTER TABLE public.knowledge_provenance
    ALTER COLUMN root_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS knowledge_provenance_root_source_idx
    ON public.knowledge_provenance (project_id, root_id, file_path);

CREATE TEMP TABLE entity_roots ON COMMIT DROP AS
SELECT
    e.id AS entity_id,
    COALESCE(ee.root_id, NULLIF(e.metadata->>'root_id', ''), 'legacy') AS root_id,
    row_number() OVER (
        PARTITION BY e.id
        ORDER BY COALESCE(ee.root_id, NULLIF(e.metadata->>'root_id', ''), 'legacy')
    ) AS root_order
FROM public.kb_entities e
LEFT JOIN public.entity_evidence ee
    ON ee.entity_id = e.id AND ee.project_id = e.project_id
GROUP BY e.id, ee.root_id;

CREATE TEMP TABLE entity_root_map (
    old_entity_id bigint NOT NULL,
    new_entity_id bigint NOT NULL,
    root_id text NOT NULL
) ON COMMIT DROP;

INSERT INTO entity_root_map (old_entity_id, new_entity_id, root_id)
SELECT entity_id, entity_id, root_id
FROM entity_roots
WHERE root_order = 1;

UPDATE public.kb_entities e
SET stable_key = pg_temp.root_scoped_key(e, roots.root_id),
    updated_at = now()
FROM entity_roots roots
WHERE e.id = roots.entity_id AND roots.root_order = 1;

WITH clones AS (
    INSERT INTO public.kb_entities (
        project_id, visibility, agent_id, session_id, name, stable_key,
        type, description, metadata, created_at, updated_at
    )
    SELECT
        e.project_id, e.visibility, e.agent_id, e.session_id, e.name,
        pg_temp.root_scoped_key(e, roots.root_id), e.type, e.description,
        e.metadata, e.created_at, now()
    FROM entity_roots roots
    JOIN public.kb_entities e ON e.id = roots.entity_id
    WHERE roots.root_order > 1
    RETURNING id, stable_key
)
INSERT INTO entity_root_map (old_entity_id, new_entity_id, root_id)
SELECT roots.entity_id, clones.id, roots.root_id
FROM entity_roots roots
JOIN public.kb_entities e ON e.id = roots.entity_id
JOIN clones ON clones.stable_key = pg_temp.root_scoped_key(e, roots.root_id)
WHERE roots.root_order > 1;

UPDATE public.entity_evidence ee
SET entity_id = map.new_entity_id,
    updated_at = now()
FROM entity_root_map map
WHERE ee.entity_id = map.old_entity_id AND ee.root_id = map.root_id;

UPDATE public.project_knowledge k
SET entity_id = map.new_entity_id
FROM entity_root_map map
WHERE k.entity_id = map.old_entity_id AND k.root_id = map.root_id;

ALTER TABLE public.kb_relations
    DROP CONSTRAINT IF EXISTS kb_relations_from_id_to_id_relation_type_key;

CREATE TEMP TABLE relation_groups ON COMMIT DROP AS
SELECT
    r.id AS relation_id,
    COALESCE(re.root_id, from_map.root_id, 'legacy') AS root_id,
    COALESCE(re.visibility, r.visibility) AS visibility,
    CASE WHEN re.id IS NULL THEN r.agent_id ELSE re.agent_id END AS agent_id,
    CASE WHEN re.id IS NULL THEN r.session_id ELSE re.session_id END AS session_id,
    row_number() OVER (
        PARTITION BY r.id
        ORDER BY
            COALESCE(re.root_id, from_map.root_id, 'legacy'),
            COALESCE(re.visibility, r.visibility),
            COALESCE(re.agent_id, ''),
            COALESCE(re.session_id, '')
    ) AS scope_order
FROM public.kb_relations r
LEFT JOIN public.relation_evidence re
    ON re.relation_id = r.id AND re.project_id = r.project_id
LEFT JOIN entity_root_map from_map ON from_map.old_entity_id = r.from_id
GROUP BY
    r.id, re.id, re.root_id, from_map.root_id, re.visibility,
    re.agent_id, re.session_id;

CREATE TEMP TABLE relation_map (
    old_relation_id bigint NOT NULL,
    new_relation_id bigint NOT NULL,
    root_id text NOT NULL,
    visibility text NOT NULL,
    agent_id text,
    session_id text
) ON COMMIT DROP;

INSERT INTO relation_map (old_relation_id, new_relation_id, root_id, visibility, agent_id, session_id)
SELECT relation_id, relation_id, root_id, visibility, agent_id, session_id
FROM relation_groups
WHERE scope_order = 1;

UPDATE public.kb_relations r
SET from_id = (
        SELECT from_map.new_entity_id FROM entity_root_map from_map
        WHERE from_map.old_entity_id = r.from_id AND from_map.root_id = groups.root_id
    ),
    to_id = (
        SELECT to_map.new_entity_id FROM entity_root_map to_map
        WHERE to_map.old_entity_id = r.to_id AND to_map.root_id = groups.root_id
    ),
    visibility = groups.visibility,
    agent_id = groups.agent_id,
    session_id = groups.session_id
FROM relation_groups groups
WHERE r.id = groups.relation_id AND groups.scope_order = 1;

WITH clones AS (
    INSERT INTO public.kb_relations (
        project_id, visibility, agent_id, session_id,
        from_id, to_id, relation_type, weight, confidence, created_at
    )
    SELECT
        r.project_id, groups.visibility, groups.agent_id, groups.session_id,
        from_map.new_entity_id, to_map.new_entity_id,
        r.relation_type, r.weight, r.confidence, r.created_at
    FROM relation_groups groups
    JOIN public.kb_relations r ON r.id = groups.relation_id
    JOIN entity_root_map from_map
        ON from_map.old_entity_id = r.from_id AND from_map.root_id = groups.root_id
    JOIN entity_root_map to_map
        ON to_map.old_entity_id = r.to_id AND to_map.root_id = groups.root_id
    WHERE groups.scope_order > 1
    RETURNING id, project_id, visibility, agent_id, session_id, from_id, to_id, relation_type
)
INSERT INTO relation_map (old_relation_id, new_relation_id, root_id, visibility, agent_id, session_id)
SELECT groups.relation_id, clones.id, groups.root_id, groups.visibility, groups.agent_id, groups.session_id
FROM relation_groups groups
JOIN public.kb_relations r ON r.id = groups.relation_id
JOIN entity_root_map from_map
    ON from_map.old_entity_id = r.from_id AND from_map.root_id = groups.root_id
JOIN entity_root_map to_map
    ON to_map.old_entity_id = r.to_id AND to_map.root_id = groups.root_id
JOIN clones
    ON clones.project_id = r.project_id
   AND clones.visibility = groups.visibility
   AND clones.agent_id IS NOT DISTINCT FROM groups.agent_id
   AND clones.session_id IS NOT DISTINCT FROM groups.session_id
   AND clones.from_id = from_map.new_entity_id
   AND clones.to_id = to_map.new_entity_id
   AND clones.relation_type = r.relation_type
WHERE groups.scope_order > 1;

UPDATE public.relation_evidence re
SET relation_id = map.new_relation_id,
    updated_at = now()
FROM relation_map map
WHERE re.relation_id = map.old_relation_id
  AND re.root_id = map.root_id
  AND re.visibility = map.visibility
  AND re.agent_id IS NOT DISTINCT FROM map.agent_id
  AND re.session_id IS NOT DISTINCT FROM map.session_id;

DO $preflight$
DECLARE
    duplicate_keys integer;
    orphan_relations integer;
    mismatched_entities integer;
BEGIN
    SELECT count(*) INTO duplicate_keys
    FROM (
        SELECT stable_key
        FROM public.kb_entities
        GROUP BY stable_key
        HAVING count(*) > 1
    ) duplicates;
    IF duplicate_keys > 0 THEN
        RAISE EXCEPTION 'Root-scoped migration produced % duplicate stable keys', duplicate_keys;
    END IF;

    SELECT count(*) INTO mismatched_entities
    FROM public.entity_evidence ee
    JOIN public.kb_entities e ON e.id = ee.entity_id AND e.project_id = ee.project_id
    WHERE e.stable_key <> pg_temp.root_scoped_key(e, ee.root_id);
    IF mismatched_entities > 0 THEN
        RAISE EXCEPTION 'Root-scoped migration left % mismatched entity evidence rows', mismatched_entities;
    END IF;

    SELECT count(*) INTO orphan_relations
    FROM public.relation_evidence re
    LEFT JOIN public.kb_relations r ON r.id = re.relation_id AND r.project_id = re.project_id
    WHERE r.id IS NULL;
    IF orphan_relations > 0 THEN
        RAISE EXCEPTION 'Root-scoped migration left % orphan relation evidence rows', orphan_relations;
    END IF;
END
$preflight$;

CREATE UNIQUE INDEX IF NOT EXISTS kb_relations_scope_unique
    ON public.kb_relations (
        from_id,
        to_id,
        relation_type,
        project_id,
        visibility,
        COALESCE(agent_id, ''),
        COALESCE(session_id, '')
    );
