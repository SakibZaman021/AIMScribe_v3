-- ================================================================
-- 006 - the doctor comes from the enrolled machine
--
-- doctor_id used to arrive from the browser. A live database shows what that
-- produces: DR001, DR_DEMO_001, DR_TEST_001, DR_DIAG_001, DR003 - five
-- "doctors" and no register. The archive is filed by doctor, so every typo
-- creates a folder nobody will open again.
--
-- aimscribe.exe runs on one doctor's PC, so the machine already identifies the
-- doctor. Binding it at enrolment - exactly as hospital_id already is - means
-- an administrator sets it once, the browser cannot influence it, and there is
-- no login, no password and no list to maintain.
--
-- The doctor register then stops being something to configure and becomes
-- something to observe: it is derived from the recordings themselves.
-- ================================================================

BEGIN;

ALTER TABLE devices           ADD COLUMN IF NOT EXISTS doctor_id TEXT;
ALTER TABLE enrollment_tokens ADD COLUMN IF NOT EXISTS doctor_id TEXT;

-- One machine, one doctor. Two devices for the same doctor is normal (a
-- consulting room and a ward laptop); one device claiming two doctors is not.
CREATE INDEX IF NOT EXISTS idx_devices_doctor
    ON devices(doctor_id, hospital_id) WHERE revoked_at IS NULL;

-- Backfill from what the sessions already recorded, where a device has only
-- ever been used by one doctor. A device with a mixed history is left NULL for
-- a human to resolve rather than guessed at.
UPDATE devices d
   SET doctor_id = s.doctor_id
  FROM (SELECT device_id, min(doctor_id) AS doctor_id
          FROM sessions
         WHERE device_id IS NOT NULL AND doctor_id IS NOT NULL
         GROUP BY device_id
        HAVING count(DISTINCT doctor_id) = 1) s
 WHERE s.device_id = d.device_id
   AND d.doctor_id IS NULL;


-- ================================================================
-- The doctor register, derived rather than configured.
--
-- "DR001 saw 5 patients today" is a question about the recordings, so it is
-- answered from the recordings.
-- ================================================================
CREATE OR REPLACE VIEW v_doctor_activity AS
SELECT
    s.hospital_id,
    s.doctor_id,
    s.session_date,
    count(*)                                    AS consultations,
    count(DISTINCT s.patient_id)                AS patients,
    min(s.start_time)                           AS first_consultation,
    max(s.end_time)                             AS last_consultation,
    round(sum(s.total_duration_seconds)::numeric / 3600, 2)  AS hours_recorded,
    round(sum(s.paused_seconds)::numeric / 60, 1)            AS minutes_paused,
    count(*) FILTER (WHERE s.archived_at IS NOT NULL)        AS archived,
    count(*) FILTER (WHERE s.status = 'quarantined')         AS quarantined
FROM sessions s
WHERE s.protocol_version = 2
  AND s.doctor_id IS NOT NULL
GROUP BY s.hospital_id, s.doctor_id, s.session_date;


-- Every doctor the system has ever seen, and whether a machine is enrolled to
-- them. A doctor appearing here with no device is a typo from the era when the
-- browser could name anyone.
CREATE OR REPLACE VIEW v_doctors AS
SELECT
    COALESCE(d.doctor_id, s.doctor_id)          AS doctor_id,
    COALESCE(d.hospital_id, s.hospital_id)      AS hospital_id,
    count(DISTINCT d.device_id)                 AS devices_enrolled,
    count(s.session_id)                         AS total_consultations,
    count(DISTINCT s.patient_id)                AS total_patients,
    min(s.session_date)                         AS first_seen,
    max(s.session_date)                         AS last_seen
FROM sessions s
FULL OUTER JOIN devices d
  ON d.doctor_id = s.doctor_id AND d.revoked_at IS NULL
WHERE COALESCE(d.doctor_id, s.doctor_id) IS NOT NULL
GROUP BY COALESCE(d.doctor_id, s.doctor_id), COALESCE(d.hospital_id, s.hospital_id);

COMMIT;
