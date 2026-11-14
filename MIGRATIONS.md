# Migrations

Versioned SQL under `migrations/` is the sole source of production DDL. `create_schema.py`, the central installer, and tests all call `migration_runner.py`; RagEngine performs read-only compatibility checks and never creates or alters schema.

## Manifest policy

`migrations/manifest.json` declares database, schema, order, required status, transaction mode, SHA-256, and schema invariants. Required is the default. A missing file, checksum mismatch, unknown metadata, or unsupported non-transactional migration stops before DDL.

Each transactional migration and its registry row commit together. On failure the current migration transaction is rolled back; already committed versions are reported as a committed boundary and destructive rollback SQL is not executed. Runtime readiness revalidates every required migration file/checksum, every required registry filename/checksum/status, and every declared schema invariant—not only the latest version.

The current manifest runs from `baseline` through `v16`. Migration `v13` adds the shared query cache and per-project generation counters and is required. Migration `v14` adds the validated local answer cache and is optional: without it, answer-cache operations fail open while retrieval and generation remain available. Migration `v15` is required: it adds `knowledge_provenance.root_id` (plus `knowledge_provenance_root_source_idx`) and scopes `kb_entities.stable_key` (`kb_entities_stable_key_unique`) and `kb_relations_scope_unique` by `root_id`. Migration `v16` (`0016_strict_owner_and_document_root_scope.sql`) is required: it enforces non-blank private owner check constraints (`project_knowledge_owner_nonblank_check`, `kb_entities_owner_nonblank_check`, `kb_relations_owner_nonblank_check`), replaces `kb_documents_project_source_unique` with `kb_documents_project_root_source_unique` on `(project_id, root_id, source_uri, file_path)`, and creates `project_knowledge_project_root_file_idx`.

Migration `v9` adds `entity_evidence` and `relation_evidence` without rewriting canonical graph facts. Its manifest fingerprint covers the mapping columns, owner/source checks, project/root foreign keys, and scope/source/identity indexes. The identity indexes include project, root, visibility, owners, and nullable source references so one canonical fact can safely carry evidence in multiple scopes.

Planning and application run under one stable, session-level PostgreSQL advisory lock. The lock is acquired with a bounded timeout before registry bootstrap or pending-version reads, remains held across per-migration commits, and is explicitly released after rollback/cleanup. A legacy `schema_migrations` table is inspected through `information_schema.columns`; missing metadata columns are added in one transaction before any query references them.

## Legacy checksums

Rows that predate the metadata bootstrap receive `checksum=NULL` and `execution_status='legacy-unverified'`. An existing registry row with an empty checksum is `legacy-unverified`, not verified. It is not reapplied and does not receive a checksum automatically.

The explicit reconciliation procedure locks the row, verifies version-specific tables/columns/constraints/indexes, and only then records the manifest checksum with `reconciliation_method='schema-fingerprint'`. Any missing invariant rolls back reconciliation.

## Fresh database test

The PostgreSQL test creates a random separate database through an administrative connection, applies all migrations twice, runs full runtime readiness, verifies `public` objects and registry checksums, closes every connection, and drops the exact generated database in `finally`. Lack of `CREATEDB`, or inability to install the required server-provided vector extension in that isolated database, is an explicit environment SKIP; the test never falls back to a schema inside `aiagent`.
