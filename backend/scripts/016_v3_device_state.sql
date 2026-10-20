-- ================================================================
-- 016 - What each recorder reports about itself, and recordings that continue
--       one another.
--
-- From each heartbeat (recorders 3.4.0 and later):
--   held_sessions / held_count   recordings the PC is holding because this
--                                server said no (failure review U3)
--   disk_encrypted               BitLocker on the drive holding its audio (X1)
--   clock_offset_seconds         server time minus the PC's time (U1)
--   last_heartbeat_at            when the last heartbeat arrived
--
-- sessions.continues_session_id: a consultation resumed after the recorder
-- crashed or the PC restarted is a second recording of the same visit (S4).
-- It points at the first, so the two are confirmed, filed and shown together.
--
-- Additive only, and safe to run twice.
-- ================================================================

BEGIN;

ALTER TABLE devices ADD COLUMN IF NOT EXISTS held_sessions        JSONB;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS held_count           INTEGER NOT NULL DEFAULT 0;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS disk_encrypted       BOOLEAN;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS clock_offset_seconds REAL;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_heartbeat_at    TIMESTAMP WITH TIME ZONE;

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS continues_session_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS idx_sessions_continues
    ON sessions (continues_session_id) WHERE continues_session_id IS NOT NULL;

COMMIT;
