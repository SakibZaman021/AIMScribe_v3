-- ================================================================
-- 034 - CMED refresh tokens
--
-- CMED asked to renew its 5-minute access token without signing a new
-- assertion every time. A token reply now also carries a refresh token:
--
--   cmed_refresh_tokens  one row per refresh token issued, by hash only.
--                        Each is spent once: using it returns a new access
--                        token AND a new refresh token (rotation), and the
--                        old row gets used_at. Every token descended from one
--                        signed assertion shares its chain_id and its
--                        expires_at, so a chain ends at a fixed time after
--                        the last signed login, however often it is used.
--                        A spent token presented again revokes its chain.
--
-- Safe to run twice. Run as the tables' owner.
-- ================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS cmed_refresh_tokens (
    token_sha256       BYTEA PRIMARY KEY,
    chain_id           UUID NOT NULL,
    client_id          VARCHAR(64) NOT NULL REFERENCES cmed_clients(client_id),
    kid                VARCHAR(128) NOT NULL,
    cmed_hospital_ids  VARCHAR(64)[] NOT NULL,
    scopes             TEXT[] NOT NULL,
    issued_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    expires_at         TIMESTAMP WITH TIME ZONE NOT NULL,
    used_at            TIMESTAMP WITH TIME ZONE,
    revoked_at         TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_cmed_refresh_chain ON cmed_refresh_tokens (chain_id);
CREATE INDEX IF NOT EXISTS idx_cmed_refresh_client ON cmed_refresh_tokens (client_id, kid);
CREATE INDEX IF NOT EXISTS idx_cmed_refresh_expiry ON cmed_refresh_tokens (expires_at);
COMMENT ON TABLE cmed_refresh_tokens IS
    'Refresh tokens issued to CMED''s servers, stored as SHA-256; each is spent once.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_api') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON cmed_refresh_tokens TO aims_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON cmed_refresh_tokens TO aims_monitor;
    END IF;
END $$;

COMMIT;
