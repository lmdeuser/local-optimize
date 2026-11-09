-- Migration v6: Stable Key for entities and non-unique display name
-- Idempotent upgrade script for PostgreSQL with pgvector

-- 1. Add stable_key column to kb_entities if not exists
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='kb_entities' AND column_name='stable_key') THEN
        ALTER TABLE kb_entities ADD COLUMN stable_key text;
    END IF;
END $$;

-- 2. Safe Backfill for existing entities without a stable_key using structural hierarchy:
-- project_id:relative_path:symbol_type:name OR fallback to legacy:<type>:<id>
UPDATE kb_entities
SET stable_key = CASE
    WHEN metadata->>'stable_key' IS NOT NULL AND metadata->>'stable_key' != '' THEN metadata->>'stable_key'
    WHEN metadata->>'relative_path' IS NOT NULL AND metadata->>'relative_path' != '' THEN
        COALESCE(metadata->>'project_name', 'legacy-project') || ':' || 
        (metadata->>'relative_path') || ':' || 
        COALESCE(type, 'concept') || ':' || 
        name
    WHEN metadata->>'file' IS NOT NULL AND metadata->>'file' != '' THEN
        COALESCE(metadata->>'project_name', 'legacy-project') || ':' || 
        (metadata->>'file') || ':' || 
        COALESCE(type, 'concept') || ':' || 
        name
    ELSE
        'legacy:' || COALESCE(type, 'concept') || ':' || id::text
END
WHERE stable_key IS NULL OR stable_key = '';

-- 3. Remove ONLY unique constraints covering strictly the 'name' column
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        WHERE t.relname = 'kb_entities'
          AND c.contype = 'u'
          AND array_length(c.conkey, 1) = 1
          AND (
              SELECT a.attname
              FROM pg_attribute a
              WHERE a.attrelid = t.oid AND a.attnum = c.conkey[1]
          ) = 'name'
    LOOP
        EXECUTE 'ALTER TABLE kb_entities DROP CONSTRAINT IF EXISTS ' || quote_ident(r.conname);
    END LOOP;
END $$;

-- 4. Preflight Check: verify zero NULLs, zero empty strings, zero duplicates
DO $$
DECLARE
    null_cnt int;
    empty_cnt int;
    dup_cnt int;
BEGIN
    SELECT count(*) INTO null_cnt FROM kb_entities WHERE stable_key IS NULL;
    SELECT count(*) INTO empty_cnt FROM kb_entities WHERE stable_key = '';
    SELECT count(*) INTO dup_cnt FROM (
        SELECT stable_key FROM kb_entities GROUP BY stable_key HAVING count(*) > 1
    ) sub;

    IF null_cnt > 0 OR empty_cnt > 0 OR dup_cnt > 0 THEN
        RAISE EXCEPTION 'Preflight check failed for kb_entities: NULL=%, Empty=%, Duplicates=%', null_cnt, empty_cnt, dup_cnt;
    END IF;
END $$;

-- 5. Add UNIQUE CONSTRAINT on stable_key and enforce NOT NULL
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints 
        WHERE table_schema=current_schema() AND table_name='kb_entities' AND constraint_name='kb_entities_stable_key_unique'
    ) THEN
        ALTER TABLE kb_entities ADD CONSTRAINT kb_entities_stable_key_unique UNIQUE (stable_key);
    END IF;
END $$;

ALTER TABLE kb_entities ALTER COLUMN stable_key SET NOT NULL;
