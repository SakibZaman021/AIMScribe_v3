-- ================================================================
-- 003 - readable clip names
--
-- Session audio and clips were identified only by the session ULID
-- (01KYH8YGWNYQD43KEFZY8YN76T), which is correct as a key but unreadable
-- to anyone auditing the archive.
--
-- Clips now carry
--     {patient}_{doctor}_{hospital}_{YYYYMMDD}_{NNNN}.wav
-- and the joined session file, written by the archive worker into
-- sessions.archive_relpath, carries
--     {patient}_{doctor}_{hospital}_{HHMM}-{HHMM}_{YYYYMMDD}.wav
--
-- The ULID remains the primary key everywhere. It is minted on the doctor's
-- PC so a session can open with the backend unreachable, and the object key
-- in storage stays ULID-based on purpose: keys reach Cloudflare access logs
-- and presigned URLs, where a patient identifier must never appear.
--
-- Additive and idempotent. Existing rows keep NULL until backfilled below.
-- ================================================================

BEGIN;

ALTER TABLE segments ADD COLUMN IF NOT EXISTS clip_name TEXT;

-- Two clips in one session must never share a display name.
CREATE UNIQUE INDEX IF NOT EXISTS idx_segments_clip_name
    ON segments(session_id, clip_name) WHERE clip_name IS NOT NULL;

-- Find a session's files by eye, which is the whole point of the change.
CREATE INDEX IF NOT EXISTS idx_segments_clip_name_lookup
    ON segments(clip_name) WHERE clip_name IS NOT NULL;

-- Backfill anything already committed. Sessions missing a patient, doctor,
-- hospital or date are left NULL rather than given a misleading name.
UPDATE segments s
   SET clip_name = concat_ws('_',
           se.patient_id, se.doctor_id, se.hospital_id,
           to_char(COALESCE(se.session_date, se.opened_at::date), 'YYYYMMDD'),
           lpad(s.seq_no::text, 4, '0')
       ) || '.wav'
  FROM sessions se
 WHERE se.session_id = s.session_id
   AND s.clip_name IS NULL
   AND se.patient_id  IS NOT NULL
   AND se.doctor_id   IS NOT NULL
   AND se.hospital_id IS NOT NULL
   AND COALESCE(se.session_date, se.opened_at::date) IS NOT NULL;

COMMIT;
