-- Stop `project_knowledge.entity_id` pointing at entities that no longer exist.
--
-- Re-indexing a document deletes the entities it had produced and recreates them
-- with fresh ids, but the `entity_id` already copied onto knowledge rows was
-- never repointed or cleared. On the live store that left 504 of 1124 rows
-- referencing deleted entities, against an empty `kb_entities`.
--
-- The damage is silent rather than loud: `search` copies `entity_id` into each
-- hit's metadata, and `agentic_search` seeds its graph traversal exclusively
-- from those ids. Every seed resolves to nothing, the traversal finds no
-- entities, and the caller receives a successful GraphRAG response with an empty
-- graph — indistinguishable from "this knowledge genuinely has no relations".

UPDATE public.project_knowledge k
SET entity_id = NULL
WHERE k.entity_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM public.kb_entities e WHERE e.id = k.entity_id
  );

-- Composite target so the link cannot cross a project boundary, matching the
-- isolation the evidence tables already enforce (migration 0009). ON DELETE SET
-- NULL is what makes re-indexing self-healing from here on: deleting an entity
-- clears the references instead of orphaning them.
--
-- The column list after SET NULL is required, not cosmetic. A bare
-- `ON DELETE SET NULL` on a composite foreign key nulls *every* referencing
-- column, which here would blank `project_id` — the hard scope filter behind all
-- retrieval and ACL predicates — on any entity deletion. Naming `entity_id`
-- confines the cascade to the link itself.
ALTER TABLE public.project_knowledge
    DROP CONSTRAINT IF EXISTS project_knowledge_entity_scope_fk;

ALTER TABLE public.project_knowledge
    ADD CONSTRAINT project_knowledge_entity_scope_fk
    FOREIGN KEY (entity_id, project_id)
    REFERENCES public.kb_entities (id, project_id)
    ON DELETE SET NULL (entity_id);
