-- ================================================================
-- aims_clinical 004 - interface version 4: one patient, one row per visit
--
--   patient          one row per patient at a hospital: what does not change
--                    from visit to visit (sex, date of birth, blood group)
--   patient_visit    one row per visit, keyed by CMED's visit_id: API 2's
--                    demographics and vitals, and API 3's sections exactly as
--                    sent (AIMS LAB's clinical layout, aims_cribe.docx)
--   visit_messages   every API 2 and API 3 of interface 4, exactly as received;
--                    a corrected prescription is the next version, the first
--                    is never overwritten
--   visit_quarantine a message that could not be filed (no visit_id, patient_id
--                    or hospital_id, or a visit_id that belongs to another
--                    patient), kept for a person to look at
--
-- Added beside the tables of 001-003, which interface 4 keeps filling too, so
-- nothing that reads them changes. Run as aims_clinical_owner
-- (deploy/uiu/postgres/migrate.sh). Safe to run twice.
-- ================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS patient (
    hospital_id         VARCHAR(64) NOT NULL,      -- CMED's hospital code
    hospital_patient_id VARCHAR(64) NOT NULL,      -- CMED's patient_id
    sex                 VARCHAR(16),
    date_of_birth       DATE,
    blood_group         VARCHAR(16),
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (hospital_id, hospital_patient_id)
);

CREATE TABLE IF NOT EXISTS patient_visit (
    id                  BIGSERIAL PRIMARY KEY,
    visit_id            VARCHAR(64) NOT NULL,
    hospital_id         VARCHAR(64) NOT NULL,
    hospital_patient_id VARCHAR(64) NOT NULL,
    aims_id             VARCHAR(64),               -- the recording's session id
    practitioner_id     VARCHAR(64),               -- CMED's doctor_id
    start_time          TIMESTAMPTZ,
    visit_date          DATE,
    age                 TEXT,                      -- as CMED shows it, "36Y 1D"
    age_years           INTEGER CHECK (age_years BETWEEN 0 AND 130),
    vitals              JSONB NOT NULL DEFAULT '[]'::jsonb,   -- API 2 observations
    chief_complaints    JSONB NOT NULL DEFAULT '[]'::jsonb,
    observations        JSONB NOT NULL DEFAULT '[]'::jsonb,
    diagnosis           JSONB NOT NULL DEFAULT '[]'::jsonb,
    lab_orders          JSONB NOT NULL DEFAULT '[]'::jsonb,
    radiology_orders    JSONB NOT NULL DEFAULT '[]'::jsonb,
    drug_orders         JSONB NOT NULL DEFAULT '[]'::jsonb,
    advice              JSONB NOT NULL DEFAULT '[]'::jsonb,
    follow_up           DATE,
    follow_up_text      TEXT,                      -- as sent, when not a date
    issued_at           TIMESTAMPTZ,
    prescription_version INTEGER NOT NULL DEFAULT 0,
    patient_information_at TIMESTAMPTZ,
    prescription_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT patient_visit_once UNIQUE (hospital_id, visit_id),
    FOREIGN KEY (hospital_id, hospital_patient_id) REFERENCES patient
);
CREATE INDEX IF NOT EXISTS idx_patient_visit_patient
    ON patient_visit (hospital_id, hospital_patient_id, visit_date);
CREATE INDEX IF NOT EXISTS idx_patient_visit_aims_id ON patient_visit (aims_id);

CREATE TABLE IF NOT EXISTS visit_messages (
    id            BIGSERIAL PRIMARY KEY,
    kind          VARCHAR(32) NOT NULL
                  CHECK (kind IN ('patient_information', 'prescription')),
    hospital_id   VARCHAR(64) NOT NULL,
    visit_id      VARCHAR(64) NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    body          JSONB NOT NULL,
    body_sha256   BYTEA NOT NULL,
    content_sha256 BYTEA NOT NULL,                 -- the body without issued_at
    warnings      JSONB NOT NULL DEFAULT '[]'::jsonb,
    client        TEXT,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT visit_message_once UNIQUE (kind, hospital_id, visit_id, body_sha256)
);
CREATE INDEX IF NOT EXISTS idx_visit_messages_visit
    ON visit_messages (hospital_id, visit_id, kind, version);

CREATE TABLE IF NOT EXISTS visit_quarantine (
    id          BIGSERIAL PRIMARY KEY,
    kind        VARCHAR(32) NOT NULL,
    raw         TEXT NOT NULL,
    problems    JSONB NOT NULL DEFAULT '[]'::jsonb,
    client      TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE patient IS
    'One row per patient at a hospital (interface 4): sex, date of birth, blood group. Never a name, phone or address.';
COMMENT ON TABLE patient_visit IS
    'One row per visit, keyed by CMED''s visit_id (interface 4): API 2''s demographics and vitals, API 3''s sections as sent.';
COMMENT ON TABLE visit_messages IS
    'Every interface-4 API 2 and API 3 exactly as received; corrections are later versions.';
COMMENT ON TABLE visit_quarantine IS
    'Interface-4 messages that could not be filed, kept for review.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_clinical_writer') THEN
        GRANT SELECT, INSERT, UPDATE ON patient, patient_visit TO aims_clinical_writer;
        GRANT SELECT, INSERT ON visit_messages, visit_quarantine TO aims_clinical_writer;
        GRANT USAGE ON SEQUENCE patient_visit_id_seq, visit_messages_id_seq,
                                visit_quarantine_id_seq TO aims_clinical_writer;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_clinical_reader') THEN
        GRANT SELECT ON patient, patient_visit, visit_messages, visit_quarantine
            TO aims_clinical_reader;
    END IF;
END $$;

COMMIT;
