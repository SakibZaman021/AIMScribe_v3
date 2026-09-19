-- ================================================================
-- 004 - object storage is transit, not storage
--
-- Clips used to stay in the bucket forever. Once the AIMS LAB server holds a
-- verified copy and purge receipts have been issued, the bucket copy has no
-- further purpose: it is cost, and it is patient audio sitting at a third
-- party for no reason.
--
-- The order that makes this safe is fixed and must not be reordered:
--     archive file written and re-hashed from disk
--         -> archive_relpath / archive_sha256 recorded
--             -> purge receipts signed
--                 -> bucket object deleted   <- only now
--                     -> agent deletes its local copy on receipt
--
-- object_deleted_at records the last step so a failed deletion can be retried
-- without guessing, and so "still in the bucket" is answerable in one query.
-- ================================================================

BEGIN;

ALTER TABLE segments ADD COLUMN IF NOT EXISTS object_deleted_at TIMESTAMP WITH TIME ZONE;

-- The retry queue: archived, receipted, but still occupying the bucket.
CREATE INDEX IF NOT EXISTS idx_segments_awaiting_object_delete
    ON segments(session_id)
    WHERE object_deleted_at IS NULL AND state = 'archived';


-- ================================================================
-- Two views, so the questions that actually get asked are one SELECT
-- rather than a join across four tables and a ULID nobody can read.
-- ================================================================

-- "Where is this patient's audio on the AIMS LAB server?"
CREATE OR REPLACE VIEW v_audio_files AS
SELECT
    s.patient_id,
    s.doctor_id,
    s.hospital_id,
    s.session_date,
    s.archive_relpath                                   AS file_location,
    encode(s.archive_sha256, 'hex')                     AS file_sha256,
    s.archive_bytes                                     AS file_bytes,
    s.segment_count                                     AS clips,
    round(s.total_duration_seconds::numeric, 1)         AS duration_seconds,
    round(s.paused_seconds, 1)                          AS paused_seconds,
    s.opened_at,
    s.closed_at,
    s.archived_at,
    s.status,
    s.quarantine_reason,
    (s.chain_verified_at IS NOT NULL)                   AS chain_verified,
    s.consent_obtained,
    s.legal_hold,
    s.retention_until,
    s.session_id                                        AS internal_key
FROM sessions s
WHERE s.protocol_version = 2;


-- "Why is there a gap in this recording, and who authorised it?"
--
-- Read straight from the signed chain rather than a summary table, so the
-- reason shown is the one the device signed at the time.
CREATE OR REPLACE VIEW v_session_pauses AS
SELECT
    s.patient_id,
    s.doctor_id,
    s.hospital_id,
    s.session_date,
    c.entry_no,
    c.entry_type,                                        -- 'pause' or 'resume'
    c.occurred_at,
    c.payload ->> 'reason'                    AS reason,
    c.payload ->> 'authorised_by'             AS authorised_by,
    (c.payload ->> 'seconds')::numeric        AS seconds,
    s.archive_relpath                         AS file_location,
    s.session_id                              AS internal_key
FROM chain_entries c
JOIN sessions s ON s.session_id = c.session_id
WHERE c.entry_type IN ('pause', 'resume')
ORDER BY s.session_date DESC, c.entry_no;

COMMIT;
