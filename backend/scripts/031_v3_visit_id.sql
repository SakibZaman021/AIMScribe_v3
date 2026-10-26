-- ================================================================
-- 031 - CMED's visit_id on the recording, and when its prescription arrived
--
--   grant_authorisations.visit_id   the visit_id API 1 carried (interface 4),
--                                   as the recorder forwarded it with the grant
--   sessions.visit_id               the recording's visit, written when it is
--                                   named; part of its file name
--   sessions.prescription_received_at
--                                   when CMED's API 3 for this recording's
--                                   visit reached the server. The heartbeat
--                                   reply tells the recorder, which ends the
--                                   recording about two minutes after it unless
--                                   the next patient is opened first. Written
--                                   once: a corrected prescription does not
--                                   move it.
--
-- Columns only, all nullable: recordings without a visit_id are unchanged.
-- Safe to run twice. Run as the tables' owner.
-- ================================================================
BEGIN;

ALTER TABLE grant_authorisations ADD COLUMN IF NOT EXISTS visit_id VARCHAR(64);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS visit_id VARCHAR(64);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS prescription_received_at TIMESTAMP WITH TIME ZONE;

CREATE INDEX IF NOT EXISTS idx_sessions_visit_id ON sessions (visit_id)
    WHERE visit_id IS NOT NULL;

COMMENT ON COLUMN sessions.visit_id IS
    'CMED''s visit_id for this recording (interface 4); part of its file name.';
COMMENT ON COLUMN sessions.prescription_received_at IS
    'When CMED''s API 3 for this visit arrived; the recorder ends the recording about two minutes later unless the next patient is opened first.';

COMMIT;
