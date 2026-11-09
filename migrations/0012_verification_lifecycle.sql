-- Give a stored fact a verification lifecycle, separate from its write time.
--
-- Ranking already decays by `created_at`, but write time is not truth: a wrong
-- chunk saved yesterday outranks a right one saved last month, and nothing in
-- the scoring notices that a fact stopped being true.
--
-- The facts most exposed to this describe *other people's* systems — DOM
-- selectors, undocumented endpoints, UI behaviour. Their expiry is invisible
-- from this machine, so it cannot be detected; it can only be declared in
-- advance and then re-confirmed.
--
--   verified_at        when someone last confirmed the fact still holds, as
--                      opposed to when it was first written. NULL means never
--                      re-confirmed, in which case created_at is the best
--                      available proxy.
--   recheck_after_days how long that confirmation is good for. NULL means the
--                      fact is not expected to rot — a principle rather than an
--                      observation of somebody else's UI.
--
-- Deliberately absent: any automatic deletion or demotion on expiry. An old fact
-- is not a wrong fact, and silently dropping it would trade a stale hit for a
-- silent miss. Expiry only raises a flag the caller can see.

ALTER TABLE public.project_knowledge
    ADD COLUMN IF NOT EXISTS verified_at timestamptz;

ALTER TABLE public.project_knowledge
    ADD COLUMN IF NOT EXISTS recheck_after_days integer;

ALTER TABLE public.project_knowledge
    DROP CONSTRAINT IF EXISTS project_knowledge_recheck_after_days_check;

ALTER TABLE public.project_knowledge
    ADD CONSTRAINT project_knowledge_recheck_after_days_check
    CHECK (recheck_after_days IS NULL OR recheck_after_days > 0);

-- Finding what is overdue must not degrade into a sequential scan of the store,
-- and only rows that declared an interval can ever be overdue.
CREATE INDEX IF NOT EXISTS project_knowledge_recheck_idx
    ON public.project_knowledge (project_id, recheck_after_days, verified_at)
    WHERE recheck_after_days IS NOT NULL;
