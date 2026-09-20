-- ================================================================
-- The tables the v2 and v3 migrations build on.
--
-- The v1 scripts (backend/scripts/init_database.sql and the migration
-- that followed it) do not replay cleanly from an empty database: they
-- disagree about the legacy transcription tables. The live database was
-- built by them and has been migrated ever since, so rather than fix
-- history, this file states what those tables look like today - which is
-- all the later migrations need.
--
-- backend/tests/test_db_integration.py reads this same file to build its
-- test database, so a new UIU server and the tests cannot drift apart.
-- ================================================================

CREATE TABLE IF NOT EXISTS sessions (
    session_id             VARCHAR(255) PRIMARY KEY,
    patient_id             VARCHAR(100) NOT NULL,
    doctor_id              VARCHAR(100) NOT NULL,
    hospital_id            VARCHAR(100) NOT NULL,
    status                 VARCHAR(20) DEFAULT 'active',
    total_clips            INTEGER DEFAULT 0,
    total_duration_seconds DECIMAL(10,2) DEFAULT 0,
    health_screening       JSONB,
    recording_date         DATE,
    start_time             TIME,
    end_time               TIME,
    created_at             TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at             TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    completed_at           TIMESTAMPTZ,
    ner_webhook_url        TEXT,
    status_webhook_url     TEXT
);

CREATE TABLE IF NOT EXISTS clips (
    id          SERIAL PRIMARY KEY,
    session_id  VARCHAR(255) REFERENCES sessions(session_id) ON DELETE CASCADE,
    clip_number INTEGER NOT NULL,
    object_key  VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS patients (
    patient_id VARCHAR(100) PRIMARY KEY
);
