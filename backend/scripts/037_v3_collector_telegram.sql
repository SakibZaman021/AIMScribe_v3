-- ============================================================
-- 037: data collectors are alerted on Telegram, and a failed alert is retried
--
-- Email is too slow to send a collector to the doctor's room, and at a
-- hundred unusual events a day too costly. A collector links their Telegram
-- once (a one-time code from the dashboard), and from then on every urgent
-- event reaches their phone at once. Email stays for invitations and the
-- end-of-shift summary.
--
-- A message that could not be sent is now tried again with growing waits,
-- so a short outage at Resend or Telegram no longer loses it.
--
-- Additions only: two new tables and three new columns. Nothing is removed.
-- ============================================================

BEGIN;

-- Which Telegram chat a collector's alerts go to. Re-linking replaces the
-- chat (an UPDATE); a link is never deleted.
CREATE TABLE IF NOT EXISTS collector_telegram (
    email      VARCHAR(254) PRIMARY KEY,
    chat_id    BIGINT NOT NULL,
    username   VARCHAR(64),
    linked_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

-- The one-time codes behind "Connect Telegram": the code travels in the
-- t.me link, and the bot sees it when the collector presses Start.
CREATE TABLE IF NOT EXISTS collector_telegram_codes (
    code        VARCHAR(48) PRIMARY KEY,
    email       VARCHAR(254) NOT NULL,
    created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    used_at     TIMESTAMP WITH TIME ZONE
);

-- How each alert went out, and when to try again if it did not.
ALTER TABLE collector_notifications ADD COLUMN IF NOT EXISTS channel VARCHAR(16);
ALTER TABLE collector_notifications ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE collector_notifications ADD COLUMN IF NOT EXISTS next_try_at TIMESTAMP WITH TIME ZONE;
CREATE INDEX IF NOT EXISTS idx_collector_notes_retry
    ON collector_notifications (next_try_at) WHERE delivered = FALSE AND next_try_at IS NOT NULL;

COMMENT ON TABLE collector_telegram IS 'The Telegram chat each data collector''s alerts go to.';
COMMENT ON TABLE collector_telegram_codes IS 'One-time codes that link a collector''s Telegram to their account.';
COMMENT ON COLUMN collector_notifications.channel IS 'telegram or email: how the alert went (or was tried).';
COMMENT ON COLUMN collector_notifications.next_try_at IS 'When an undelivered alert is tried again; empty once sent or given up.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_api') THEN
        GRANT SELECT, INSERT, UPDATE ON collector_telegram, collector_telegram_codes TO aims_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON collector_telegram TO aims_monitor;
    END IF;
END $$;

COMMIT;
