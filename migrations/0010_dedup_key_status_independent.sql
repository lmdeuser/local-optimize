-- Take `validation_status` out of the knowledge dedup key.
--
-- The key used to be sha256 over
--     project_id | content_hash | visibility | owner_agent | owner_session | validation_status
-- which made the same text at two statuses two different rows. Marking a fact
-- deprecated therefore inserted a twin and left the original live and
-- retrievable — the correction path in `RagEngine.save` corrected nothing.
--
-- Existing rows carry keys computed with the old formula, so they must be
-- recomputed here; otherwise the first re-save of already-stored content would
-- miss the ON CONFLICT target and insert a duplicate instead of updating.

-- Fail closed rather than silently discarding knowledge: if any two rows would
-- collapse onto one key, an operator has to decide which fact survives.
DO $$
DECLARE
    collisions bigint;
BEGIN
    SELECT count(*) INTO collisions FROM (
        SELECT 1
        FROM public.project_knowledge
        GROUP BY project_id, content_hash, visibility,
                 CASE WHEN visibility IN ('agent-private', 'session-private')
                      THEN COALESCE(agent_id, '') ELSE '' END,
                 CASE WHEN visibility = 'session-private'
                      THEN COALESCE(session_id, '') ELSE '' END
        HAVING count(*) > 1
    ) AS duplicated;

    IF collisions > 0 THEN
        RAISE EXCEPTION
            'dedup key migration would merge % group(s) of rows that differ only by validation_status; resolve them before migrating', collisions;
    END IF;
END
$$;

UPDATE public.project_knowledge
SET dedup_key = encode(
        sha256(
            convert_to(
                concat_ws(
                    E'\x1f',
                    project_id,
                    COALESCE(content_hash, md5(content)),
                    visibility,
                    CASE WHEN visibility IN ('agent-private', 'session-private')
                         THEN COALESCE(agent_id, '') ELSE '' END,
                    CASE WHEN visibility = 'session-private'
                         THEN COALESCE(session_id, '') ELSE '' END
                ),
                'UTF8'
            )
        ),
        'hex'
    );
