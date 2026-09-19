-- ================================================================
-- 007 - readable object keys
--
-- Clips were stored as audio/<ulid>/seg_00001.wav. That kept patient
-- identifiers out of Cloudflare's access logs, but it also made the R2 console
-- unusable: every folder is 26 random characters and there is no way to tell
-- which patient's consultation you are about to download.
--
-- Keys now carry the same identifiers the archive filename does:
--
--     audio/10045_DR001_HOSP001_0930_20260728/
--         10045_DR001_HOSP001_20260728_0001.wav
--
-- The prefix is computed once at session open, from the hospital's local clock,
-- and stored here. Segment authorisation builds keys from it and commit
-- validates against it, so a client cannot choose where its audio lands - which
-- is what the ULID prefix was protecting.
--
-- The trade accepted knowingly: object keys appear in Cloudflare's access logs
-- and in presigned URLs, so patient identifiers now reach a third party's logs.
-- Sessions opened before this keep their ULID keys and still archive correctly.
-- ================================================================

BEGIN;

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS object_prefix TEXT;

-- Find a session from a key seen in the storage console.
CREATE INDEX IF NOT EXISTS idx_sessions_object_prefix
    ON sessions(object_prefix) WHERE object_prefix IS NOT NULL;

-- Backfill what can be derived. Sessions missing a component are left NULL and
-- keep their existing ULID-based keys rather than being given a name that does
-- not match the objects actually in storage.
UPDATE sessions s
   SET object_prefix = concat_ws('_',
           s.patient_id, s.doctor_id, s.hospital_id,
           to_char(s.start_time, 'HH24MI'),
           to_char(COALESCE(s.session_date, s.opened_at::date), 'YYYYMMDD'))
 WHERE s.protocol_version = 2
   AND s.object_prefix IS NULL
   AND s.patient_id  IS NOT NULL
   AND s.doctor_id   IS NOT NULL
   AND s.hospital_id IS NOT NULL
   AND s.start_time  IS NOT NULL
   AND COALESCE(s.session_date, s.opened_at::date) IS NOT NULL;

COMMIT;
