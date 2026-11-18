-- ================================================================
-- 047 - a paused recording is seen; one Telegram card per problem
--
-- 1. sessions.paused_since / pause_reason: set when the doctor pauses a
--    recording (the pause reaches the server within a second), cleared on
--    Resume and when it ends. The dashboard shows "Paused since 13:32
--    (sensitive personal matter)" from these, and the alerts read them.
--    The pause itself was already kept, signed, in chain_entries.
--
-- 2. collector_notifications.tg_chat_id / tg_message_id / card_sha: the
--    Telegram message an alert became, so the server can edit it in place
--    - one card per problem, updated, instead of a new message for every
--    consultation (18 Nov: one unplugged Jabra made 38 messages for one
--    researcher in two hours).
--
-- 3. telegram_boards: the one pinned "live" message per researcher, per
--    hospital, per shift.
--
-- Adds only; nothing is deleted or changed. Safe to run twice.
-- ================================================================
BEGIN;

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS paused_since TIMESTAMP WITH TIME ZONE;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pause_reason TEXT;
COMMENT ON COLUMN sessions.paused_since IS
    'While the doctor has the recording paused: when the pause began. Empty when recording or ended.';
COMMENT ON COLUMN sessions.pause_reason IS
    'The reason the doctor chose for the pause now running.';

ALTER TABLE collector_notifications ADD COLUMN IF NOT EXISTS tg_chat_id BIGINT;
ALTER TABLE collector_notifications ADD COLUMN IF NOT EXISTS tg_message_id BIGINT;
ALTER TABLE collector_notifications ADD COLUMN IF NOT EXISTS card_sha TEXT;
COMMENT ON COLUMN collector_notifications.tg_message_id IS
    'The Telegram message this alert became, so its card can be edited as the problem changes.';

CREATE TABLE IF NOT EXISTS telegram_boards (
    email        TEXT NOT NULL,
    hospital_id  TEXT NOT NULL,
    shift_key    TEXT NOT NULL,             -- 2026-11-18:morning
    chat_id      BIGINT NOT NULL,
    message_id   BIGINT NOT NULL,
    text_sha     TEXT,
    created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (email, hospital_id, shift_key)
);
COMMENT ON TABLE telegram_boards IS
    'The pinned live board each researcher gets per hospital and shift, edited as things change.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_api') THEN
        GRANT SELECT, INSERT, UPDATE ON telegram_boards TO aims_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON telegram_boards TO aims_monitor;
    END IF;
END $$;

COMMIT;
