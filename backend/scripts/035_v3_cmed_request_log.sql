-- ================================================================
-- 035 - CMED's request log
--
-- CMED asked to see, without asking AIMS LAB, what happened to each request
-- its servers sent: the token API, API 2, API 3, status. One row per request:
-- when, from which address, which API, the HTTP status, the reply's code and
-- reason, and the request id - never a message body, a token or a clinical
-- value. CMED reads its own rows with GET /api/v4/clinical/log (cmed_log.py).
-- Rows older than 30 days are removed as new ones are written.
--
-- Safe to run twice. Run as the tables' owner.
-- ================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS cmed_request_log (
    id          BIGSERIAL PRIMARY KEY,
    at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    client_id   VARCHAR(64),                    -- NULL: the caller could not be told
    ip          VARCHAR(64) NOT NULL,
    method      VARCHAR(8) NOT NULL,
    path        VARCHAR(200) NOT NULL,
    api         VARCHAR(40) NOT NULL,
    status      SMALLINT NOT NULL,
    code        VARCHAR(64),
    reason      VARCHAR(500),
    visit_id    VARCHAR(64),
    request_id  VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS idx_cmed_log_client ON cmed_request_log (client_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_cmed_log_ip ON cmed_request_log (ip, id DESC) WHERE client_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_cmed_log_at ON cmed_request_log (at);
COMMENT ON TABLE cmed_request_log IS
    'Every request CMED''s servers sent and its outcome, kept 30 days; no bodies, no tokens.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_api') THEN
        GRANT SELECT, INSERT, DELETE ON cmed_request_log TO aims_api;
        GRANT USAGE, SELECT ON SEQUENCE cmed_request_log_id_seq TO aims_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON cmed_request_log TO aims_monitor;
    END IF;
END $$;

COMMIT;
