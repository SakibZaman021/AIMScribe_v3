-- ================================================================
-- 027 - who downloaded which recording from the portal
--
--   portal_downloads   one row per download: the person (Google email),
--                      the recording and part, the copy it came from, when
--                      it started and ended, and whether the whole file
--                      arrived. Written by the server; nobody edits it
--
-- A download is decrypted from the cloud copy by the archive worker and
-- streamed to the browser (backend/src/downloads.py); nothing decrypted is
-- kept. This table, and the audit log, are the record of who took what.
--
-- Safe to run twice. Run as the tables' owner.
-- ================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS portal_downloads (
    id           BIGSERIAL PRIMARY KEY,
    email        TEXT NOT NULL REFERENCES portal_people (email),
    session_id   VARCHAR(64) NOT NULL,
    part_no      INTEGER,
    object_key   TEXT NOT NULL,
    started_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    finished_at  TIMESTAMP WITH TIME ZONE,
    outcome      VARCHAR(16) NOT NULL DEFAULT 'started'
                 CHECK (outcome IN ('started', 'completed', 'failed')),
    bytes        BIGINT,
    address      TEXT,
    reason       TEXT
);
CREATE INDEX IF NOT EXISTS idx_portal_downloads_email
    ON portal_downloads (email, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_portal_downloads_session
    ON portal_downloads (session_id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON portal_downloads TO aims_monitor;
    END IF;
    IF to_regclass('public.portal_downloads') IS NOT NULL
       AND pg_has_role(current_user, (SELECT relowner FROM pg_class
                                      WHERE oid = to_regclass('public.portal_downloads')), 'USAGE') THEN
        COMMENT ON TABLE portal_downloads IS
            'Every recording downloaded from the portal: who (Google email), which recording and part, when, and whether the whole file arrived.';
    END IF;
END $$;

COMMIT;
