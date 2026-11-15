-- ============================================================
-- 039: who to call, and problems that last
--
-- Every alert a researcher gets about their hospitals now says who the data
-- collector on shift is, with their phone and Telegram, so the researcher
-- can call them. A problem that is still there 30, 60 and 90 minutes after
-- the alert is sent again, to the collector and the researchers, and the
-- researchers hear when it is fixed. For that:
--
--   portal_people.phone                  a collector's phone number
--   collector_notifications.resolved_at  when the problem behind an alert
--                                        was found fixed (or ended)
--   collector_notifications.resolution   how: "the Jabra is back in use" ...
--
-- Alerts sent before this are marked as not followed, so the first round
-- does not send "fixed" for hours-old problems.
--
-- Additions only: three new columns. Nothing is deleted.
-- ============================================================

BEGIN;

ALTER TABLE portal_people ADD COLUMN IF NOT EXISTS phone VARCHAR(32);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'collector_notifications'
                      AND column_name = 'resolved_at') THEN
        ALTER TABLE collector_notifications ADD COLUMN resolved_at TIMESTAMP WITH TIME ZONE;
        ALTER TABLE collector_notifications ADD COLUMN IF NOT EXISTS resolution VARCHAR(200);
        UPDATE collector_notifications
           SET resolved_at = now(), resolution = 'sent before problems were followed'
         WHERE resolved_at IS NULL;
    END IF;
END $$;
ALTER TABLE collector_notifications ADD COLUMN IF NOT EXISTS resolution VARCHAR(200);

CREATE INDEX IF NOT EXISTS idx_collector_notes_open_problems
    ON collector_notifications (created_at)
    WHERE resolved_at IS NULL AND audience = 'collector';

COMMENT ON COLUMN portal_people.phone IS 'A data collector''s phone number, given to the researchers who look after their hospitals.';
COMMENT ON COLUMN collector_notifications.resolved_at IS 'When the problem behind an alert was found fixed or ended.';
COMMENT ON COLUMN collector_notifications.resolution IS 'How the problem behind the alert ended.';

COMMIT;
