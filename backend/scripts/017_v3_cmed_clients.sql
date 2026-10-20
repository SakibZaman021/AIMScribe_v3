-- ================================================================
-- 017 - CMED's identity, one client per hospital (SMART Backend Services)
--
-- A single X-CMED-Key spoke for every hospital CMED serves: one leak and
-- anyone could write API 2 / API 3 for all of them and read any patient's
-- visit list. Now each hospital is its own client:
--
--   cmed_clients        who may speak, for which CMED hospital ids, from
--                       which addresses, until when
--   cmed_client_keys    the client's PUBLIC keys (JWK). The private key never
--                       leaves CMED; a copy of this database gives nothing
--                       that can sign.
--   cmed_assertion_jti  every signed assertion seen, until it expires, so
--                       none can be used twice
--   cmed_access_tokens  the 5-minute tokens issued, by hash only
--
-- The old static keys stay possible during the switch, but only bound to a
-- hospital (api_keys.cmed_hospital_ids) and only while AIMS_CMED_LEGACY_KEYS
-- is not "off".
-- ================================================================

CREATE TABLE IF NOT EXISTS cmed_clients (
    client_id          VARCHAR(64) PRIMARY KEY
                       CHECK (client_id ~ '^[A-Za-z0-9_.-]{3,64}$'),
    label              TEXT NOT NULL,
    cmed_hospital_ids  VARCHAR(64)[] NOT NULL CHECK (cardinality(cmed_hospital_ids) >= 1),
    scopes             TEXT[] NOT NULL DEFAULT ARRAY['system/clinical.write',
                                                     'system/Encounter.read'],
    allowed_cidrs      TEXT[],                          -- NULL: any address
    created_by         TEXT NOT NULL,
    created_at         TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    expires_at         TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at         TIMESTAMP WITH TIME ZONE,
    last_used_at       TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS cmed_client_keys (
    client_id   VARCHAR(64) NOT NULL REFERENCES cmed_clients(client_id),
    kid         VARCHAR(128) NOT NULL,
    alg         VARCHAR(16) NOT NULL CHECK (alg IN ('ES384', 'RS384', 'ES256')),
    public_jwk  JSONB NOT NULL,
    created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (client_id, kid)
);

CREATE TABLE IF NOT EXISTS cmed_assertion_jti (
    client_id   VARCHAR(64) NOT NULL,
    jti         VARCHAR(256) NOT NULL,
    expires_at  TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (client_id, jti)
);
CREATE INDEX IF NOT EXISTS idx_cmed_assertion_expiry ON cmed_assertion_jti (expires_at);

CREATE TABLE IF NOT EXISTS cmed_access_tokens (
    token_sha256       BYTEA PRIMARY KEY,
    client_id          VARCHAR(64) NOT NULL REFERENCES cmed_clients(client_id),
    cmed_hospital_ids  VARCHAR(64)[] NOT NULL,
    scopes             TEXT[] NOT NULL,
    issued_at          TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    expires_at         TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at         TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_cmed_tokens_expiry ON cmed_access_tokens (expires_at);

-- The interim static keys: each one names the hospitals it may speak for.
-- A key with none named is refused (clinical.py), so every key issued before
-- this migration stops working until it is re-issued for one hospital.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS cmed_hospital_ids VARCHAR(64)[];

-- The operator lists clients and keys in the audit log; nothing here holds a
-- secret, so no column needs hiding.
