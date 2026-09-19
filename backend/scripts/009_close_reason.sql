-- ================================================================
-- 009 - why a recording ended
--
-- A consultation can end four ways: the doctor presses Stop in CMED, someone
-- stops it from the agent's tray icon, a new patient supersedes it, or the PC
-- dies and the session is closed short on the next start.
--
-- Only the first is routine. The reason was written into the signed chain and
-- into the agent's log, and went no further - so a recording force-stopped on a
-- doctor's PC looked, from here, exactly like one that finished normally.
--
-- It is now stored on the session and raised as an alert when it is anything
-- but a normal stop.
-- ================================================================

BEGIN;

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS close_reason TEXT;

-- Recordings that did not end at the doctor's hand, newest first. This is the
-- list to look at each morning.
DROP VIEW IF EXISTS v_abnormal_closes;
CREATE VIEW v_abnormal_closes AS
SELECT
    s.session_id,
    s.hospital_id,
    s.doctor_id,
    s.patient_id,
    s.close_reason,
    s.session_date,
    s.recording_date,
    s.start_time,
    s.end_time,
    s.total_duration_seconds,
    s.segment_count,
    s.status,
    s.archived_at
FROM sessions s
WHERE s.closed_at IS NOT NULL
  AND coalesce(s.close_reason, '') NOT IN ('doctor_stopped', '')
ORDER BY s.closed_at DESC;

COMMIT;
