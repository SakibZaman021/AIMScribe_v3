-- ================================================================
-- 010 - SRS 3.2 on the existing server: grants from the server,
-- confirmation against CMED's API 2, Channel B, and refusals.
--
-- Additive only. Existing sessions become confirmation = 'legacy' and
-- keep archiving exactly as before.
-- ================================================================

BEGIN;

-- CMED's own clinic identifier, mapped once per clinic (SRS-ENR-19).
ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS cmed_hospital_id VARCHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hospitals_cmed_id
    ON hospitals(cmed_hospital_id) WHERE cmed_hospital_id IS NOT NULL;

-- One row per grant the server issues (SRS 3.2 §5). Links the recorder's
-- request to CMED's API 2 and, later, to the session it opened.
CREATE TABLE IF NOT EXISTS grant_authorisations (
    jti              TEXT PRIMARY KEY,
    device_id        UUID NOT NULL REFERENCES devices(device_id),
    hospital_id      VARCHAR(64) NOT NULL,          -- AIMS LAB code, from the device
    cmed_hospital_id VARCHAR(64) NOT NULL,          -- as CMED sent it
    patient_id       VARCHAR(64) NOT NULL,
    doctor_id        VARCHAR(64) NOT NULL,
    start_time       TEXT NOT NULL,                 -- character for character (SRS-CNF-02)
    visit_date       DATE NOT NULL,
    notice_id        BIGINT,                        -- the API 2 that confirmed it
    session_id       VARCHAR(64),
    issued_at        TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    expires_at       TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_grants_waiting
    ON grant_authorisations(patient_id, doctor_id, cmed_hospital_id, start_time, visit_date)
    WHERE notice_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_grants_session ON grant_authorisations(session_id);

-- Channel B records: API 2 (patient_information) and prescriptions.
-- Kept here until the clinical database is split out (Phase 5).
CREATE TABLE IF NOT EXISTS clinical_records (
    id               BIGSERIAL PRIMARY KEY,
    kind             VARCHAR(24) NOT NULL
        CHECK (kind IN ('patient_information', 'prescription')),
    cmed_hospital_id VARCHAR(64) NOT NULL,
    hospital_id      VARCHAR(64),                   -- mapped; NULL if unmapped
    patient_id       VARCHAR(64) NOT NULL,
    doctor_id        VARCHAR(64) NOT NULL,
    start_time       TEXT NOT NULL,
    visit_date       DATE NOT NULL,
    version          INTEGER NOT NULL DEFAULT 1,
    body             JSONB NOT NULL,
    body_sha256      BYTEA NOT NULL,
    claimed_by_jti   TEXT,                          -- API 2 only: the grant it confirmed
    received_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    CONSTRAINT clinical_body_sha_len CHECK (octet_length(body_sha256) = 32),
    CONSTRAINT clinical_once UNIQUE (kind, body_sha256)          -- SRS-CHB-07
);
CREATE INDEX IF NOT EXISTS idx_clinical_visit
    ON clinical_records(kind, patient_id, doctor_id, cmed_hospital_id, start_time, visit_date);
CREATE INDEX IF NOT EXISTS idx_clinical_unclaimed
    ON clinical_records(received_at) WHERE kind = 'patient_information'
                                       AND claimed_by_jti IS NULL;

-- Requests that failed validation, stored unchanged (SRS-CRI-07, SRS-CHB-10).
CREATE TABLE IF NOT EXISTS clinical_quarantine (
    id          BIGSERIAL PRIMARY KEY,
    kind        VARCHAR(24) NOT NULL,
    raw         TEXT NOT NULL,
    problems    JSONB NOT NULL,
    received_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMP WITH TIME ZONE
);

-- Confirmation state per session (SRS §5.6).
--   legacy       opened by a protocol-2 recorder; archived as before
--   pending      waiting for CMED's API 2
--   confirmed    matched; admitted to the dataset
--   unconfirmed  no API 2 after two minutes; kept out of the dataset
--   refused      the patient did not consent; erased (SRS §7.8a)
--   expired      unconfirmed for 24 hours; erased (SRS-CNF-09)
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS grant_jti      TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS confirmation   VARCHAR(16) NOT NULL DEFAULT 'legacy';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS unconfirmed_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS refused_at     TIMESTAMP WITH TIME ZONE;
DO $$ BEGIN
    ALTER TABLE sessions ADD CONSTRAINT sessions_confirmation_valid CHECK (
        confirmation IN ('legacy','pending','confirmed','unconfirmed','refused','expired'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS idx_sessions_confirmation
    ON sessions(confirmation, opened_at) WHERE confirmation IN ('pending','unconfirmed');

-- A refused consultation keeps no patient identifier (SRS-CNS-06). The visit
-- is remembered only as a digest, so clinical data CMED sends later for the
-- same visit can still be recognised and dropped (SRS-CNS-04).
CREATE TABLE IF NOT EXISTS session_refusals (
    session_id   VARCHAR(64) PRIMARY KEY,
    device_id    UUID REFERENCES devices(device_id),
    hospital_id  VARCHAR(64),
    doctor_id    VARCHAR(64),
    visit_sha256 BYTEA,
    refused_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    completed_at TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_refusals_visit ON session_refusals(visit_sha256);

COMMIT;
