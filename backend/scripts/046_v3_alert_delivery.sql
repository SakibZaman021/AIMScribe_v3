-- ================================================================
-- 046 - when each alert reached its person, and refused recordings ended
--
-- 1. collector_notifications.delivered_at: the moment Telegram (or mail)
--    accepted the alert. With created_at and happened_at it shows how fast
--    alerts reach people - per shift on the dashboard and in the summaries.
--    Empty for alerts sent before this, and for any not delivered.
--
-- 2. A recording the patient refused was erased but never marked as ended,
--    so the alert rules took it for one still running: on 16-17 Nov eleven
--    refused recordings drew 283 "running 60 min / still not fixed / not
--    seen" alerts. The server now ends a recording when it erases it
--    (db_v2.erase_session); this gives the same end to the ones refused
--    before, at the moment they were refused. Nothing is deleted: two empty
--    fields of those rows are filled.
--
-- Safe to run twice. Run as the tables' owner.
-- ================================================================
BEGIN;

ALTER TABLE collector_notifications
    ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMP WITH TIME ZONE;

COMMENT ON COLUMN collector_notifications.delivered_at IS
    'When the alert was accepted by Telegram or mail; with created_at and happened_at, how fast it reached the person. Empty before 18 Nov 2026 and while undelivered.';

UPDATE sessions
   SET closed_at = COALESCE(refused_at, updated_at, now()),
       close_reason = COALESCE(close_reason, 'doctor_stopped:patient_did_not_consent')
 WHERE closed_at IS NULL AND confirmation = 'refused';

COMMIT;
