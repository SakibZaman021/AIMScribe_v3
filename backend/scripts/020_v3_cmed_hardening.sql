-- ================================================================
-- 020 - CMED's credentials, after review
--
--   cmed_access_tokens.kid        which key bought a token, so revoking a
--                                 stolen key also stops its live tokens
--   cmed_clients.identifier_systems
--                                 a hospital's own identifier systems, for
--                                 FHIR resources that carry several ids
--   cmed_clients.scopes           SMART v2 names (system/Bundle.c,
--                                 system/Encounter.rs); the old names stay
--                                 accepted as aliases in the code
--   fhir_messages                 every message Bundle.id seen, with the
--                                 fingerprint of what it carried: the same id
--                                 with different content is an alert
-- Safe to run twice.
-- ================================================================

ALTER TABLE cmed_access_tokens ADD COLUMN IF NOT EXISTS kid VARCHAR(128);
CREATE INDEX IF NOT EXISTS idx_cmed_tokens_kid ON cmed_access_tokens (client_id, kid)
    WHERE revoked_at IS NULL;

ALTER TABLE cmed_clients ADD COLUMN IF NOT EXISTS identifier_systems JSONB;

ALTER TABLE cmed_clients ALTER COLUMN scopes
    SET DEFAULT ARRAY['system/Bundle.c', 'system/Encounter.rs'];
UPDATE cmed_clients
   SET scopes = ARRAY(
       SELECT DISTINCT CASE s
                WHEN 'system/clinical.write' THEN 'system/Bundle.c'
                WHEN 'system/Encounter.read' THEN 'system/Encounter.rs'
                ELSE s END
         FROM unnest(scopes) AS s)
 WHERE scopes && ARRAY['system/clinical.write', 'system/Encounter.read']::text[];

CREATE TABLE IF NOT EXISTS fhir_messages (
    client_id     VARCHAR(64) NOT NULL,
    bundle_id     VARCHAR(128) NOT NULL,
    body_sha256   BYTEA NOT NULL,
    first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, bundle_id)
);
CREATE INDEX IF NOT EXISTS idx_fhir_messages_seen ON fhir_messages (first_seen_at);
