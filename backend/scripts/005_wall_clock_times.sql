-- ================================================================
-- 005 - the times a human reads
--
-- opened_at and closed_at are UTC timestamps, which is right for arithmetic
-- and wrong for reading: at UTC+6 a 14:32 consultation shows as 08:32, and it
-- does not match the 1432-1522 in its own filename.
--
-- sessions already has recording_date / start_time / end_time from v1. Protocol
-- 2 never filled them, so the Neon console showed no end time at all. They are
-- now populated at close, in the hospital's timezone, from the same instants
-- the filename is built from.
--
-- This file backfills what is already closed and rebuilds the view to show
-- start, end and the filename together.
-- ================================================================

BEGIN;

UPDATE sessions s
   SET recording_date = (s.opened_at AT TIME ZONE h.timezone)::date,
       start_time     = (s.opened_at AT TIME ZONE h.timezone)::time,
       end_time       = (s.closed_at AT TIME ZONE h.timezone)::time
  FROM hospitals h
 WHERE h.hospital_id = s.hospital_id
   AND s.protocol_version = 2
   AND s.opened_at IS NOT NULL
   AND s.closed_at IS NOT NULL
   AND (s.end_time IS NULL OR s.start_time IS NULL OR s.recording_date IS NULL);


-- "Where is this patient's audio, and when was it recorded?"
--
-- Dropped rather than replaced: CREATE OR REPLACE cannot add columns in the
-- middle of the list, and start_time / end_time belong next to the date.
DROP VIEW IF EXISTS v_audio_files;

CREATE VIEW v_audio_files AS
SELECT
    s.patient_id,
    s.doctor_id,
    s.hospital_id,
    s.session_date,

    -- Local wall clock. These are the values in the filename.
    s.start_time,
    s.end_time,
    to_char(s.start_time, 'HH24MI') || '-' || to_char(s.end_time, 'HH24MI') AS clock,

    -- Everything the filename encodes, rebuilt from the row, so the name can be
    -- checked against the record rather than trusted.
    s.patient_id || '_' || s.doctor_id || '_' || s.hospital_id || '_' ||
        to_char(s.start_time, 'HH24MI') || '-' || to_char(s.end_time, 'HH24MI') || '_' ||
        to_char(s.session_date, 'YYYYMMDD') || '.wav'          AS expected_filename,

    s.archive_relpath                                          AS file_location,
    encode(s.archive_sha256, 'hex')                            AS file_sha256,
    s.archive_bytes                                            AS file_bytes,
    s.segment_count                                            AS clips,
    round(s.total_duration_seconds::numeric, 1)                AS duration_seconds,
    round(s.paused_seconds, 1)                                 AS paused_seconds,

    s.opened_at,                                               -- UTC
    s.closed_at,                                               -- UTC
    s.archived_at,
    s.status,
    s.quarantine_reason,
    (s.chain_verified_at IS NOT NULL)                          AS chain_verified,
    s.consent_obtained,
    s.legal_hold,
    s.retention_until,
    s.session_id                                               AS internal_key
FROM sessions s
WHERE s.protocol_version = 2;

COMMIT;
