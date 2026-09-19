-- ================================================================
-- aims_clinical - who the patient was, what was measured, and what
-- was prescribed (SRS 3.2 §8.7.2).
--
-- A separate database from aims_recordings, with separate credentials
-- (SRS-DBA-20). The two share only patient_id and the recording's file
-- name (SRS-DBA-21). Apply to the aims_clinical database; roles are in
-- 002_roles.sql.
-- ================================================================

BEGIN;

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- ----------------------------------------------------------------
-- Everything CMED sends, exactly as sent (SRS-CRI-06, SRS-DBA-11).
-- The tables below are loaded from here; a field not modelled yet is
-- never lost, and a load that failed can be run again (SRS-CRI-04).
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS intake_records (
    id               BIGSERIAL PRIMARY KEY,
    kind             VARCHAR(24) NOT NULL
        CHECK (kind IN ('patient_information', 'prescription')),
    cmed_hospital_id VARCHAR(64) NOT NULL,
    hospital_id      VARCHAR(64),
    patient_id       VARCHAR(64) NOT NULL,
    doctor_id        VARCHAR(64) NOT NULL,
    start_time       TEXT NOT NULL,
    visit_date       DATE NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    body             JSONB NOT NULL,
    body_sha256      BYTEA NOT NULL CHECK (octet_length(body_sha256) = 32),
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    loaded_at        TIMESTAMPTZ,
    load_error       TEXT,
    CONSTRAINT intake_once UNIQUE (kind, body_sha256)             -- SRS-CHB-07
);
CREATE INDEX IF NOT EXISTS idx_intake_visit
    ON intake_records (kind, patient_id, doctor_id, cmed_hospital_id, start_time, visit_date);
CREATE INDEX IF NOT EXISTS idx_intake_unloaded ON intake_records (id) WHERE loaded_at IS NULL;

-- Requests that failed validation, unchanged (SRS-CRI-07, SRS-CHB-10).
CREATE TABLE IF NOT EXISTS intake_quarantine (
    id          BIGSERIAL PRIMARY KEY,
    kind        VARCHAR(24) NOT NULL,
    raw         TEXT NOT NULL,
    problems    JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ
);

-- ----------------------------------------------------------------
-- Patients: one table, one row each (SRS-DBA-05).
-- (patient_id, sex) is a key so the female and male tables can require
-- the right sex (SRS-DBA-06).
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS patients (
    patient_id    VARCHAR(64) PRIMARY KEY,
    sex           VARCHAR(8) NOT NULL DEFAULT 'unknown'
        CHECK (sex IN ('female', 'male', 'unknown')),
    full_name     TEXT,
    date_of_birth DATE,
    phone         TEXT,
    address       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT patients_id_sex UNIQUE (patient_id, sex)
);

-- One row per visit. 'live' is a consultation that was recorded;
-- 'previous_visit' is an older prescription CMED sent in API 2.
CREATE TABLE IF NOT EXISTS encounters (
    encounter_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id             VARCHAR(64) NOT NULL
        REFERENCES patients (patient_id) ON UPDATE CASCADE,
    doctor_id              VARCHAR(64) NOT NULL,
    hospital_id            VARCHAR(64),                 -- AIMS LAB code
    cmed_hospital_id       VARCHAR(64) NOT NULL,
    start_time             TEXT NOT NULL,
    visit_date             DATE NOT NULL,
    source                 VARCHAR(16) NOT NULL
        CHECK (source IN ('live', 'previous_visit')),
    file_stem              TEXT,                        -- shared with aims_recordings
    session_id             VARCHAR(64),
    patient_information_id BIGINT REFERENCES intake_records (id) ON DELETE SET NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT encounter_once UNIQUE
        (patient_id, doctor_id, cmed_hospital_id, start_time, visit_date, source)
);
CREATE INDEX IF NOT EXISTS idx_encounters_patient
    ON encounters (patient_id, visit_date, start_time);
CREATE UNIQUE INDEX IF NOT EXISTS idx_encounters_file_stem
    ON encounters (file_stem) WHERE file_stem IS NOT NULL;

-- Details as CMED sent them that day, so older visits still show what was
-- true then.
CREATE TABLE IF NOT EXISTS encounter_demographics (
    encounter_id  UUID PRIMARY KEY REFERENCES encounters ON DELETE CASCADE,
    full_name     TEXT,
    sex           VARCHAR(8),
    age_years     SMALLINT CHECK (age_years BETWEEN 0 AND 130),
    date_of_birth DATE,
    phone         TEXT,
    address       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The measurements taken before the patient came in. The full list is
-- OD-16; anything not yet a column is kept in `other`.
CREATE TABLE IF NOT EXISTS paramedic_observations (
    encounter_id  UUID PRIMARY KEY REFERENCES encounters ON DELETE CASCADE,
    recorded_at   TIMESTAMPTZ,
    weight_kg     NUMERIC(5, 1) CHECK (weight_kg > 0 AND weight_kg < 500),
    height_cm     NUMERIC(5, 1) CHECK (height_cm > 0 AND height_cm < 300),
    blood_pressure TEXT,
    systolic      SMALLINT CHECK (systolic BETWEEN 30 AND 300),
    diastolic     SMALLINT CHECK (diastolic BETWEEN 10 AND 250),
    pulse_bpm     SMALLINT CHECK (pulse_bpm BETWEEN 10 AND 300),
    temperature_c NUMERIC(4, 1) CHECK (temperature_c BETWEEN 25 AND 45),
    spo2_percent  SMALLINT CHECK (spo2_percent BETWEEN 0 AND 100),
    notes         TEXT,
    other         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Fields that apply to one sex only. The database refuses a row for a
-- patient of the other sex, whatever the application does (SRS-DBA-06).
-- The fields themselves are OD-15; until agreed they are kept in `details`.
CREATE TABLE IF NOT EXISTS female_details (
    encounter_id UUID PRIMARY KEY REFERENCES encounters ON DELETE CASCADE,
    patient_id   VARCHAR(64) NOT NULL,
    sex          VARCHAR(8) NOT NULL DEFAULT 'female' CHECK (sex = 'female'),
    details      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (patient_id, sex) REFERENCES patients (patient_id, sex) ON UPDATE CASCADE
);

CREATE TABLE IF NOT EXISTS male_details (
    encounter_id UUID PRIMARY KEY REFERENCES encounters ON DELETE CASCADE,
    patient_id   VARCHAR(64) NOT NULL,
    sex          VARCHAR(8) NOT NULL DEFAULT 'male' CHECK (sex = 'male'),
    details      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (patient_id, sex) REFERENCES patients (patient_id, sex) ON UPDATE CASCADE
);

-- ----------------------------------------------------------------
-- Prescriptions: every visit and every version kept (SRS-DBA-07), one
-- row per medicine (SRS-DBA-10), the original alongside (SRS-DBA-11).
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prescriptions (
    prescription_id BIGSERIAL PRIMARY KEY,
    encounter_id    UUID NOT NULL REFERENCES encounters ON DELETE CASCADE,
    version         INTEGER NOT NULL CHECK (version > 0),
    issued_at       TIMESTAMPTZ,
    advice          TEXT,
    follow_up_date  DATE,
    notes           TEXT,
    raw_json        JSONB NOT NULL,
    intake_id       BIGINT REFERENCES intake_records (id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT prescription_version_once UNIQUE (encounter_id, version)
);

CREATE TABLE IF NOT EXISTS prescription_items (
    prescription_id BIGINT NOT NULL REFERENCES prescriptions ON DELETE CASCADE,
    line_no         INTEGER NOT NULL CHECK (line_no > 0),
    drug            TEXT NOT NULL CHECK (btrim(drug) <> ''),
    dose            TEXT,
    frequency       TEXT,
    duration        TEXT,
    instructions    TEXT,
    PRIMARY KEY (prescription_id, line_no)
);

CREATE TABLE IF NOT EXISTS diagnoses (
    prescription_id BIGINT NOT NULL REFERENCES prescriptions ON DELETE CASCADE,
    line_no         INTEGER NOT NULL CHECK (line_no > 0),
    text            TEXT NOT NULL,
    code            TEXT,
    PRIMARY KEY (prescription_id, line_no)
);

CREATE TABLE IF NOT EXISTS investigations (
    prescription_id BIGINT NOT NULL REFERENCES prescriptions ON DELETE CASCADE,
    line_no         INTEGER NOT NULL CHECK (line_no > 0),
    name            TEXT NOT NULL,
    PRIMARY KEY (prescription_id, line_no)
);

-- ----------------------------------------------------------------
-- Who read which patient, and when (SRS-DBA-18). Rows are never
-- changed or removed.
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clinical_access_log (
    id         BIGSERIAL PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    patient_id VARCHAR(64),
    detail     JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE OR REPLACE FUNCTION clinical_access_log_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'clinical_access_log is append-only; % is not permitted', TG_OP;
END $$;

DROP TRIGGER IF EXISTS clinical_access_log_no_change ON clinical_access_log;
CREATE TRIGGER clinical_access_log_no_change
    BEFORE UPDATE OR DELETE ON clinical_access_log
    FOR EACH ROW EXECUTE FUNCTION clinical_access_log_append_only();

-- updated_at, maintained by the database (SRS-DBA-14).
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['patients', 'encounters', 'encounter_demographics',
                             'paramedic_observations', 'female_details', 'male_details',
                             'prescriptions']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I_touch ON %I', t, t);
        EXECUTE format('CREATE TRIGGER %I_touch BEFORE UPDATE ON %I '
                       'FOR EACH ROW EXECUTE FUNCTION touch_updated_at()', t, t);
    END LOOP;
END $$;

-- ----------------------------------------------------------------
-- Current and previous prescription, keyed by patient_id (SRS-DBA-08).
-- Views over every visit, so a third visit never overwrites the first.
-- ----------------------------------------------------------------

-- The latest version of each visit's prescription.
CREATE OR REPLACE VIEW latest_prescriptions AS
SELECT DISTINCT ON (p.encounter_id)
       e.patient_id, e.encounter_id, e.visit_date, e.start_time, e.source,
       e.doctor_id, e.hospital_id, e.file_stem,
       p.prescription_id, p.version, p.issued_at, p.advice, p.follow_up_date,
       p.notes, p.raw_json
  FROM prescriptions p
  JOIN encounters e USING (encounter_id)
 ORDER BY p.encounter_id, p.version DESC;

-- The patient's most recent recorded visit.
CREATE OR REPLACE VIEW current_encounter AS
SELECT DISTINCT ON (patient_id) *
  FROM encounters
 WHERE source = 'live'
 ORDER BY patient_id, visit_date DESC, start_time DESC;

-- That visit's prescription, once it is built.
CREATE OR REPLACE VIEW current_prescription AS
SELECT l.*
  FROM latest_prescriptions l
  JOIN current_encounter c USING (encounter_id);

-- The latest prescription from any visit strictly before the current one,
-- so a second visit on the same day never returns the visit in progress
-- (SRS-DBA-09).
CREATE OR REPLACE VIEW previous_prescription AS
SELECT DISTINCT ON (l.patient_id) l.*
  FROM latest_prescriptions l
  JOIN current_encounter c ON c.patient_id = l.patient_id
 WHERE (l.visit_date, l.start_time) < (c.visit_date, c.start_time)
 ORDER BY l.patient_id, l.visit_date DESC, l.start_time DESC;

COMMIT;
