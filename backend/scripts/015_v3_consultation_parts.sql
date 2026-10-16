-- ================================================================
-- 015 - One recording, labelled patient by patient.
--
-- CMED's page sends a start for every profile the doctor clicks. While a
-- consultation has no prescription yet the recorder keeps recording -
-- the click may be a glance, a wrong profile corrected, or the next
-- patient after a follow-up that needed no prescription - and signs a
-- 'marker' into the chain saying who was opened, and when, to the
-- second of audio. When the recording closes this server reads those
-- markers with the prescriptions and decides who each stretch of audio
-- belongs to; the archive then cuts the audio there, one file per
-- patient. Nothing is deleted: the signed recording is kept intact and
-- the files are cut from it.
--
--   session_clicks      every marker: a patient opened, a prescription
--                       built, at which second of the joined audio
--   consultation_parts  the verdict: which patient owns which seconds,
--                       by which rule, and whether a person should look
--
-- Additive only: every existing entry type and column is kept.
-- ================================================================

BEGIN;

ALTER TABLE chain_entries DROP CONSTRAINT IF EXISTS chain_entries_entry_type_check;
ALTER TABLE chain_entries ADD CONSTRAINT chain_entries_entry_type_check
    CHECK (entry_type IN ('open', 'segment', 'pause', 'resume', 'reassign',
                          'marker', 'close'));

CREATE TABLE IF NOT EXISTS session_clicks (
    session_id        VARCHAR(64) NOT NULL,
    entry_no          INTEGER NOT NULL,
    event             VARCHAR(32) NOT NULL
                      CHECK (event IN ('patient_opened', 'prescription_built')),
    audio_seconds     DOUBLE PRECISION NOT NULL CHECK (audio_seconds >= 0),
    occurred_at       TIMESTAMP WITH TIME ZONE NOT NULL,
    patient_id        VARCHAR(64) NOT NULL,
    doctor_id         VARCHAR(64) NOT NULL,
    cmed_hospital_id  VARCHAR(64) NOT NULL,
    start_time        TEXT NOT NULL,
    visit_date        DATE NOT NULL,
    grant_jti         VARCHAR(128),
    received_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, entry_no)
);
CREATE INDEX IF NOT EXISTS idx_session_clicks_grant ON session_clicks (grant_jti);
CREATE INDEX IF NOT EXISTS idx_session_clicks_visit
    ON session_clicks (patient_id, doctor_id, cmed_hospital_id, start_time, visit_date);

CREATE TABLE IF NOT EXISTS consultation_parts (
    session_id        VARCHAR(64) NOT NULL,
    part_no           INTEGER NOT NULL CHECK (part_no >= 1),
    start_seconds     DOUBLE PRECISION NOT NULL CHECK (start_seconds >= 0),
    end_seconds       DOUBLE PRECISION,               -- NULL: to the end
    patient_id        VARCHAR(64) NOT NULL,
    doctor_id         VARCHAR(64) NOT NULL,
    cmed_hospital_id  VARCHAR(64) NOT NULL,
    start_time        TEXT NOT NULL,
    visit_date        DATE NOT NULL,
    grant_jti         VARCHAR(128),
    rule              VARCHAR(32) NOT NULL,
    needs_review      BOOLEAN NOT NULL DEFAULT false,
    file_stem         TEXT,
    local_start       TIME,
    local_end         TIME,
    archive_relpath   TEXT,
    archive_sha256    BYTEA,
    archive_bytes     BIGINT,
    copied_at         TIMESTAMP WITH TIME ZONE,
    resolved_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, part_no)
);
CREATE INDEX IF NOT EXISTS idx_consultation_parts_grant ON consultation_parts (grant_jti);

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS parts_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS parts_stale BOOLEAN NOT NULL DEFAULT false;

COMMIT;
