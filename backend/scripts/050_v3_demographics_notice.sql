-- ================================================================
-- 050 - when CMED's API 2 reached an open recording (25 Nov 2026)
--
-- Recorder 3.9.8 offers the doctor "Patient did not consent" only until
-- CMED's API 2 (demographics) for the visit arrives. The server notes the
-- moment on the recording and says so in its next heartbeat reply, as it
-- already does for API 3 (prescription_received_at).
--
-- Adds only; nothing is deleted or changed. Safe to run twice.
-- ================================================================
BEGIN;

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS demographics_received_at TIMESTAMP WITH TIME ZONE;

COMMENT ON COLUMN sessions.demographics_received_at IS
    'Recorder 3.9.8+: when CMED''s API 2 for this recording arrived while it was open.';

COMMIT;
