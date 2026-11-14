# Database schema

The only production database is the configured agent database `aiagent`; managed objects are schema-qualified in `public`.

## Core tables

- `schema_migrations`: version, filename, checksum, execution status, and reconciliation audit fields.
- `project_knowledge`: canonical content chunks with project/root identity, strict security/validation fields, embedding, FTS data, and `dedup_key`.
- `kb_documents`: project-scoped document identity and current source/file path (migration `v16` replaces `kb_documents_project_source_unique` with `kb_documents_project_root_source_unique` on `(project_id, root_id, source_uri, file_path)`).
- `knowledge_provenance`: many-to-many references from canonical chunks to documents/sources/locators. Equal content from different documents retains every reference.
- `kb_entities`: canonical semantic graph entities with stable keys and display names.
- `kb_relations`: canonical semantic edges using entity primary keys. Composite foreign keys prevent cross-project traversal.
- `entity_evidence`, `relation_evidence`: project/root-isolated source mappings carrying visibility, owner, validation, active-state, and provenance metadata for canonical graph facts.
- `kb_feedback_logs`, `agent_sessions`, and `agent_messages`: scoped operational records.
- `rag_query_cache` and `rag_cache_generation` (migration v13): shared exact-query responses and per-project invalidation generations.
- `rag_local_answer_cache` (optional migration v14): validated local answers, reused only inside the same authorized scope and corpus generation.
- Migration `v16` (required, `0016_strict_owner_and_document_root_scope.sql`): enforces non-blank private owner check constraints (`project_knowledge_owner_nonblank_check`, `kb_entities_owner_nonblank_check`, `kb_relations_owner_nonblank_check`), scopes `kb_documents` uniqueness by `root_id` (`kb_documents_project_root_source_unique`), and adds `project_knowledge_project_root_file_idx`.

## Deduplication

The canonical content key is SHA-256 over:

`project_id, content_hash, visibility, owner_agent, owner_session`

`validation_status` is deliberately excluded: changing a fact's standing must update the existing row rather than create a duplicate. `project_knowledge.dedup_key` is unique. Save uses `INSERT … ON CONFLICT`; it then inserts a separate provenance reference keyed by document/source/path/locators. Source identity is not discarded when content is reused.

`memory_optimize` is project-scoped and report-only/safe-no-delete. It never globally deletes rows or treats matching text alone as permission to discard provenance.

## Graph isolation

Traversal seeds and visited/frontier sets use entity IDs. Display names are output only. Evidence ACL does not duplicate or redefine canonical graph facts: the same entity or relation may have evidence in multiple project/root/visibility owner scopes. Composite document/chunk foreign keys bind evidence to the matching `project_id` and `root_id`; owner checks require exactly the identities permitted by each visibility. Global display-name caches are not used.
