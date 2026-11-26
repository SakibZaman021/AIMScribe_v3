-- ================================================================
-- 053 - the analysis pipeline (27 Nov 2026)
--
-- Each confirmed recording's audio copy goes to the lab's GPU machine for a
-- Bengali transcript and named-entity recognition (NER); the NER and CMED's
-- prescription then go to the quality (metrics) service. Outside machines
-- never hold a bucket key: they claim jobs here with signed requests, receive
-- the audio sealed to their own key, and post results that this server checks
-- against a signed job ticket before the archive worker seals them into the
-- recording's own folder.
--
--   pipeline_machines  the lab and metrics machines: role, public keys, revocation
--   pipeline_nonces    each signed request's nonce, used once
--   pipeline_jobs      one 'lab' job per audio copy, one 'compare' job after it
--   pipeline_results   transcript, NER and metrics as received, then copied
--
-- Adds only; nothing is deleted. Safe to run twice.
-- ================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS pipeline_machines (
    machine_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL UNIQUE,
    role          TEXT NOT NULL CHECK (role IN ('lab', 'metrics')),
    sign_pubkey   BYTEA NOT NULL CHECK (octet_length(sign_pubkey) = 32),
    box_pubkey    BYTEA CHECK (box_pubkey IS NULL OR octet_length(box_pubkey) = 32),
    allowed_ips   TEXT[],
    max_inflight  INTEGER NOT NULL DEFAULT 32 CHECK (max_inflight BETWEEN 1 AND 256),
    enrolled_by   TEXT NOT NULL,
    enrolled_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMP WITH TIME ZONE,
    revoked_by    TEXT,
    last_seen_at  TIMESTAMP WITH TIME ZONE,
    last_address  TEXT,
    CONSTRAINT pipeline_lab_has_box_key CHECK (role <> 'lab' OR box_pubkey IS NOT NULL)
);
COMMENT ON TABLE pipeline_machines IS
    'Outside machines of the analysis pipeline (053): the lab GPU machine and the '
    'metrics service. Ed25519 key signs requests; X25519 key receives sealed audio.';

CREATE TABLE IF NOT EXISTS pipeline_nonces (
    machine_id UUID NOT NULL REFERENCES pipeline_machines(machine_id),
    nonce      TEXT NOT NULL,
    used_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (machine_id, nonce)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_nonces_used ON pipeline_nonces (used_at);

CREATE TABLE IF NOT EXISTS pipeline_jobs (
    job_id          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('lab', 'compare')),
    rec             TEXT NOT NULL,                 -- pseudonymous recording id
    session_id      VARCHAR(64) NOT NULL,
    part_no         INTEGER,
    copy_object_key TEXT NOT NULL,                 -- the audio copy this run works from
    audio_sha256    BYTEA NOT NULL,                -- the archived WAV, the ticket's hash
    audio_bytes     BIGINT,
    run             INTEGER NOT NULL DEFAULT 1,
    state           TEXT NOT NULL DEFAULT 'ready'
                    CHECK (state IN ('ready', 'leased', 'done', 'parked')),
    machine_id      UUID REFERENCES pipeline_machines(machine_id),
    lease_until     TIMESTAMP WITH TIME ZONE,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_try_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_error      TEXT,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    done_at         TIMESTAMP WITH TIME ZONE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_jobs_copy_run
    ON pipeline_jobs (kind, copy_object_key, run);
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_ready
    ON pipeline_jobs (kind, next_try_at) WHERE state = 'ready';
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_leased
    ON pipeline_jobs (lease_until) WHERE state = 'leased';
CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_session ON pipeline_jobs (session_id);

CREATE TABLE IF NOT EXISTS pipeline_results (
    job_id          TEXT NOT NULL REFERENCES pipeline_jobs(job_id),
    kind            TEXT NOT NULL CHECK (kind IN ('transcript', 'ner', 'metrics')),
    body            JSONB NOT NULL,
    body_sha256     BYTEA NOT NULL,
    model           TEXT,
    model_version   TEXT,
    weights_sha256  TEXT,
    machine_id      UUID REFERENCES pipeline_machines(machine_id),
    received_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    copy_object_key TEXT,
    copied_at       TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (job_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_results_uncopied
    ON pipeline_results (received_at) WHERE copied_at IS NULL;

COMMIT;
