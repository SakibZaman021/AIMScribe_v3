-- ================================================================
-- 051 - how each clinic's recordings end (25 Nov 2026)
--
-- Recorder 3.9.8 is told, in every heartbeat reply, how recordings end at
-- its clinic:
--   'api3' - two minutes after CMED's API 3 (the prescription). Amader
--            Susastho always; every clinic until CMED's API 4 is live there.
--            An API 4 that comes anyway is logged and changes nothing.
--   'api4' - when CMED's API 4 closes the visit (15 minutes after API 3 if
--            no API 4 comes).
-- Before, a PC switched itself to API 4 for good the first time any API 4
-- reached it. Now AIMS LAB switches a clinic, when CMED says API 4 is live
-- there:   UPDATE hospitals SET end_rule = 'api4' WHERE hospital_id = '...';
--
-- Adds only; nothing is deleted. Safe to run twice.
-- ================================================================
BEGIN;

ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS end_rule TEXT NOT NULL DEFAULT 'api3';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'hospitals_end_rule_known') THEN
        ALTER TABLE hospitals ADD CONSTRAINT hospitals_end_rule_known
            CHECK (end_rule IN ('api3', 'api4'));
    END IF;
END $$;

COMMENT ON COLUMN hospitals.end_rule IS
    'How recordings end at this clinic (recorder 3.9.8+): api3 = two minutes after '
    'CMED''s API 3; api4 = on CMED''s API 4. Sent to each PC in the heartbeat reply.';

COMMIT;
