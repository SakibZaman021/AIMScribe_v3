-- ============================================================
-- 042: the device log - what every PC was told, and why
--
-- A PC that cannot join, or is refused later, is told the reason in the
-- server's reply - and until now only the PC kept it. Here every attempt a
-- PC makes to join (with a token, or by itself), and every refusal the
-- server sends a PC afterwards (recording, upload, heartbeat...), is kept:
-- when, which PC and from where, what it was doing, and the exact code and
-- message it was sent. The same refusal repeated within ten minutes adds to
-- one line (repeats, last_at) instead of a line every heartbeat.
--
-- Admins only: the dashboard shows it under CMED > Device log, and the
-- researchers' database logins are refused this table
-- (scripts/roles/researcher_logins_recordings.sql).
--
-- Additions only: one new table. Nothing is deleted, here or later.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS device_events (
    id                BIGSERIAL PRIMARY KEY,
    first_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_at           TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    repeats           INTEGER NOT NULL DEFAULT 1,
    step              VARCHAR(60) NOT NULL,
    method            VARCHAR(8),
    path              VARCHAR(200),
    status            INTEGER NOT NULL,
    code              VARCHAR(64),
    message           TEXT,
    device_id         VARCHAR(64),
    machine_name      VARCHAR(128),
    app_version       VARCHAR(32),
    address           VARCHAR(64),
    hospital_id       VARCHAR(64),
    cmed_hospital_id  VARCHAR(64),
    request_id        VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS idx_device_events_last ON device_events (last_at DESC);
CREATE INDEX IF NOT EXISTS idx_device_events_device ON device_events (device_id, last_at DESC);
CREATE INDEX IF NOT EXISTS idx_device_events_address ON device_events (address, last_at DESC);

COMMENT ON TABLE device_events IS 'What every PC was told: each join attempt, and each refusal sent to a PC, with the exact code and message. Admins only. Nothing is deleted.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_api') THEN
        GRANT SELECT, INSERT, UPDATE ON device_events TO aims_api;
        GRANT USAGE, SELECT ON SEQUENCE device_events_id_seq TO aims_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_researchers') THEN
        REVOKE ALL ON device_events FROM aims_researchers;
    END IF;
END $$;

COMMIT;
