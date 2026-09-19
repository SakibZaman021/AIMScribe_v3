-- ================================================================
-- 011 - the recording's name, in columns (SRS 3.2 SRS-SES-05, SRS-DBA-25).
--
--     PatientID_DoctorID_HospitalID_HHMMSS_HHMMSS_YYYYMMDD
--
-- The same name is used for the WAV and JSON on the archive and for
-- aims_clinical.encounters.file_stem, which is how the two databases meet
-- (SRS-DBA-21). Its parts are columns of their own: patient_id, doctor_id,
-- hospital_id and session_date already exist; the local times are new.
-- ================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS file_stem   TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS local_start TIME;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS local_end   TIME;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_file_stem
    ON sessions(file_stem) WHERE file_stem IS NOT NULL;

-- Search by any part of the name (SRS-DBA-24).
CREATE INDEX IF NOT EXISTS idx_sessions_file_stem_trgm
    ON sessions USING gin (file_stem gin_trgm_ops);

-- Clinic, then doctor, then date, then patient (SRS-DBA-02).
CREATE INDEX IF NOT EXISTS idx_sessions_hierarchy
    ON sessions(hospital_id, doctor_id, session_date, patient_id);

COMMIT;
