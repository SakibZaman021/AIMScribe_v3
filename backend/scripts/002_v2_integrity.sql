-- ================================================================
-- AIMScribe v2 - integrity, device identity, and archive tracking
--
-- Additive migration. Nothing in 001 is dropped or altered, so the
-- existing transcription and NER pipeline keeps working unchanged
-- while agents migrate to protocol 2.
--
-- Run:  psql "$DATABASE_URL" -f scripts/002_v2_integrity.sql
-- ================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ================================================================
-- HOSPITALS - tenancy. Previously only a free-text column on sessions.
-- ================================================================
CREATE TABLE IF NOT EXISTS hospitals (
    hospital_id  VARCHAR(64) PRIMARY KEY,
    name         TEXT NOT NULL,
    timezone     TEXT NOT NULL DEFAULT 'Asia/Dhaka',
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);


-- ================================================================
-- DEVICES - one row per installed agent.
--
-- This is what stops a client asserting its own hospital. The archive
-- tree is hospital-first, so a spoofed value misfiles the record.
-- ================================================================
CREATE TABLE IF NOT EXISTS devices (
    device_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hospital_id      VARCHAR(64) NOT NULL REFERENCES hospitals(hospital_id),
    -- Ed25519 public key (raw, 32 bytes). Verifies every chain entry.
    tpm_pubkey       BYTEA NOT NULL,
    -- SHA-256 of the device's bearer token, issued once at enrollment.
    -- Render terminates TLS itself and does not offer client certificates, so
    -- the device token is the transport credential; cert_fingerprint stays for
    -- deployments that do put mTLS in front (a self-hosted proxy, or a VPN).
    token_sha256     BYTEA UNIQUE,
    cert_fingerprint BYTEA UNIQUE,
    machine_name     TEXT,
    os_version       TEXT,
    app_version      TEXT,
    protocol_version INTEGER NOT NULL DEFAULT 2,
    enrolled_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_seen_at     TIMESTAMP WITH TIME ZONE,
    spool_bytes      BIGINT,
    pending_segments INTEGER,
    revoked_at       TIMESTAMP WITH TIME ZONE,
    revoked_reason   TEXT,
    CONSTRAINT devices_pubkey_len CHECK (octet_length(tpm_pubkey) = 32)
);

CREATE INDEX IF NOT EXISTS idx_devices_hospital ON devices(hospital_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON devices(last_seen_at DESC NULLS LAST);


-- ================================================================
-- ENROLLMENT TOKENS - single use, admin-issued.
-- Enrollment is never self-service; this is where a device's hospital
-- is fixed.
-- ================================================================
CREATE TABLE IF NOT EXISTS enrollment_tokens (
    token_sha256 BYTEA PRIMARY KEY,
    hospital_id  VARCHAR(64) NOT NULL REFERENCES hospitals(hospital_id),
    created_by   TEXT NOT NULL,
    created_at   TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at   TIMESTAMP WITH TIME ZONE NOT NULL,
    used_at      TIMESTAMP WITH TIME ZONE,
    device_id    UUID REFERENCES devices(device_id),
    CONSTRAINT enrollment_token_len CHECK (octet_length(token_sha256) = 32)
);


-- ================================================================
-- SESSIONS - v2 columns.
--
-- 001 defines session_id as UUID; protocol 2 agents mint ULIDs locally
-- so recording can start with the backend unreachable. Migration 001
-- (001_session_id_varchar.sql) already widens it to VARCHAR - that must
-- be applied before this file.
-- ================================================================
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS device_id         UUID REFERENCES devices(device_id);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS protocol_version  INTEGER NOT NULL DEFAULT 1;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS session_date      DATE;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS opened_at         TIMESTAMP WITH TIME ZONE;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS closed_at         TIMESTAMP WITH TIME ZONE;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS paused_seconds    NUMERIC(10,2) DEFAULT 0;

-- Audio contract, recorded per session so the archive is self-describing.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS sample_rate       INTEGER;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS channels          SMALLINT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS sample_width      SMALLINT;

-- Integrity
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS segment_count     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS chain_head_hash   BYTEA;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS chain_verified_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS quarantine_reason TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS manifest          JSONB;

-- Consent. A v2 session cannot open without it.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS consent_obtained  BOOLEAN;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS consent_method    TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS consent_at        TIMESTAMP WITH TIME ZONE;

-- R6: resolve a database row to a file on the AIMS LAB server.
-- Relative to the archive root, never absolute, so remounting the volume
-- does not invalidate every row.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS archive_relpath   TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS archive_sha256    BYTEA;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS archive_bytes     BIGINT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS archived_at       TIMESTAMP WITH TIME ZONE;

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS retention_until   DATE;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS legal_hold        BOOLEAN NOT NULL DEFAULT FALSE;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_archive_relpath
    ON sessions(archive_relpath) WHERE archive_relpath IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_archive_lookup
    ON sessions(hospital_id, doctor_id, session_date);
CREATE INDEX IF NOT EXISTS idx_sessions_patient_date
    ON sessions(patient_id, session_date DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_device
    ON sessions(device_id, opened_at DESC);
-- Drives the archive worker's queue.
CREATE INDEX IF NOT EXISTS idx_sessions_pending_archive
    ON sessions(closed_at) WHERE archived_at IS NULL AND closed_at IS NOT NULL;


-- ================================================================
-- CHAIN ENTRIES - one hash chain per session covering open, every
-- segment, every pause and resume, and close.
--
-- Pauses live in the same chain as audio, which is what makes an
-- authorised gap provable rather than merely asserted.
-- ================================================================
CREATE TABLE IF NOT EXISTS chain_entries (
    session_id       VARCHAR(64) NOT NULL,
    entry_no         INTEGER NOT NULL CHECK (entry_no >= 0),
    entry_type       VARCHAR(16) NOT NULL
        CHECK (entry_type IN ('open','segment','pause','resume','close')),
    payload          JSONB NOT NULL,
    payload_sha256   BYTEA NOT NULL,
    prev_hash        BYTEA,
    entry_hash       BYTEA NOT NULL,
    device_signature BYTEA,
    occurred_at      TIMESTAMP WITH TIME ZONE,
    recorded_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (session_id, entry_no),
    CONSTRAINT chain_hash_len CHECK (
        octet_length(payload_sha256) = 32 AND octet_length(entry_hash) = 32
    )
);

CREATE INDEX IF NOT EXISTS idx_chain_type ON chain_entries(session_id, entry_type);


-- ================================================================
-- SEGMENTS - protocol 2 replacement for `clips`.
--
-- `clips` stays untouched so the existing transcription pipeline keeps
-- working; segments carries the integrity and archive state that clips
-- has no place for.
-- ================================================================
CREATE TABLE IF NOT EXISTS segments (
    session_id        VARCHAR(64) NOT NULL,
    seq_no            INTEGER NOT NULL CHECK (seq_no > 0),
    entry_no          INTEGER NOT NULL,
    object_key        TEXT NOT NULL UNIQUE,
    bytes             BIGINT NOT NULL CHECK (bytes > 0),
    duration_seconds  NUMERIC(8,2) NOT NULL,
    sha256            BYTEA NOT NULL,
    rms_mean          REAL,
    captured_start_at TIMESTAMP WITH TIME ZONE,
    captured_end_at   TIMESTAMP WITH TIME ZONE,
    committed_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    archived_at       TIMESTAMP WITH TIME ZONE,
    is_final          BOOLEAN NOT NULL DEFAULT FALSE,
    state             VARCHAR(16) NOT NULL DEFAULT 'committed'
        CHECK (state IN ('committed','archived','purged','quarantined')),
    PRIMARY KEY (session_id, seq_no),
    FOREIGN KEY (session_id, entry_no) REFERENCES chain_entries(session_id, entry_no),
    CONSTRAINT segments_sha_len CHECK (octet_length(sha256) = 32)
);

CREATE INDEX IF NOT EXISTS idx_segments_state ON segments(state) WHERE state <> 'purged';


-- ================================================================
-- PURGE RECEIPTS - signed proof an archive copy exists and hashes
-- correctly. The agent will not delete a local file without one.
-- ================================================================
CREATE TABLE IF NOT EXISTS purge_receipts (
    session_id  VARCHAR(64) NOT NULL,
    scope       VARCHAR(16) NOT NULL CHECK (scope IN ('segment','session')),
    seq_no      INTEGER,
    sha256      BYTEA NOT NULL,
    payload     JSONB NOT NULL,
    signature   BYTEA NOT NULL,
    issued_at   TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT purge_scope_seq CHECK (
        (scope = 'segment' AND seq_no IS NOT NULL) OR
        (scope = 'session'  AND seq_no IS NULL)
    ),
    CONSTRAINT purge_sig_len CHECK (octet_length(signature) = 64)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_purge_unique
    ON purge_receipts(session_id, scope, COALESCE(seq_no, -1));


-- ================================================================
-- AUDIT LOG - append-only, hash-chained.
-- This is what makes a recording defensible months later.
-- ================================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    event_type  TEXT NOT NULL,
    actor_type  TEXT NOT NULL CHECK (actor_type IN ('device','doctor','service','admin')),
    actor_id    TEXT,
    device_id   UUID REFERENCES devices(device_id),
    session_id  VARCHAR(64),
    detail      JSONB NOT NULL DEFAULT '{}',
    prev_hash   BYTEA,
    entry_hash  BYTEA NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_audit_event   ON audit_log(event_type, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_device  ON audit_log(device_id, occurred_at DESC);

CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only; % is not permitted', TG_OP;
END;
$$;

DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log;
CREATE TRIGGER audit_log_no_change
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();

DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_append_only();


-- ================================================================
-- INTEGRITY ALERTS - what an operator has to look at.
-- ================================================================
CREATE TABLE IF NOT EXISTS integrity_alerts (
    id          BIGSERIAL PRIMARY KEY,
    raised_at   TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    session_id  VARCHAR(64),
    device_id   UUID REFERENCES devices(device_id),
    alert_type  TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    detail      JSONB NOT NULL DEFAULT '{}',
    resolved_at TIMESTAMP WITH TIME ZONE,
    resolved_by TEXT,
    resolution  TEXT
);

CREATE INDEX IF NOT EXISTS idx_alerts_open
    ON integrity_alerts(raised_at DESC) WHERE resolved_at IS NULL;


-- ================================================================
-- USED GRANTS - single-use enforcement for CMED recording grants.
-- ================================================================
CREATE TABLE IF NOT EXISTS used_grants (
    jti        TEXT PRIMARY KEY,
    session_id VARCHAR(64),
    used_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_used_grants_expiry ON used_grants(expires_at);


-- ================================================================
-- API KEYS - the backend currently has no authentication at all.
-- ================================================================
CREATE TABLE IF NOT EXISTS api_keys (
    key_sha256  BYTEA PRIMARY KEY,
    label       TEXT NOT NULL,
    scope       TEXT NOT NULL CHECK (scope IN ('agent','cmed','admin','worker')),
    hospital_id VARCHAR(64) REFERENCES hospitals(hospital_id),
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at  TIMESTAMP WITH TIME ZONE,
    last_used_at TIMESTAMP WITH TIME ZONE,
    revoked_at  TIMESTAMP WITH TIME ZONE
);


-- ================================================================
-- R6 lookup: patient -> file on the AIMS LAB server
-- ================================================================
CREATE OR REPLACE VIEW patient_recordings AS
SELECT s.patient_id,
       s.session_id,
       s.hospital_id,
       s.doctor_id,
       s.session_date,
       s.opened_at,
       s.closed_at,
       s.total_duration_seconds,
       s.paused_seconds,
       s.segment_count,
       s.archive_relpath,
       encode(s.archive_sha256, 'hex') AS archive_sha256_hex,
       s.archived_at,
       s.status,
       s.quarantine_reason
FROM sessions s;

COMMIT;
