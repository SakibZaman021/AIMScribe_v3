-- ================================================================
-- 030 - a circuit breaker on the automatic erasing of recordings
--
-- Recordings CMED never confirms are erased after AIMS_UNCONFIRMED_ERASE_DAYS
-- (025). That is right for a stray click and wrong for a fault: a bug in
-- confirmation, or CMED sending the wrong clinic code, would make most of a
-- clinic's recordings "unconfirmed", and the sweep would erase them all,
-- correctly by its own rules.
--
-- So the sweep stops for a clinic when far more of its recordings would go
-- than ever should, and says so as a critical alert (api_v2.py). An admin who
-- has looked releases it for that clinic for a few hours:
--
--   erase_releases   one row per release: the clinic, until when, who, why
--
-- Safe to run twice. Run as the tables' owner.
-- ================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS erase_releases (
    id           BIGSERIAL PRIMARY KEY,
    hospital_id  VARCHAR(64) NOT NULL,
    released_by  TEXT NOT NULL,
    reason       TEXT NOT NULL,
    released_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    until        TIMESTAMP WITH TIME ZONE NOT NULL,
    CHECK (until > released_at)
);
CREATE INDEX IF NOT EXISTS idx_erase_releases_clinic
    ON erase_releases (hospital_id, until DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON erase_releases TO aims_monitor;
    END IF;
    IF to_regclass('public.erase_releases') IS NOT NULL
       AND pg_has_role(current_user, (SELECT relowner FROM pg_class
                                      WHERE oid = to_regclass('public.erase_releases')), 'USAGE') THEN
        COMMENT ON TABLE erase_releases IS
            'An admin letting the automatic erasing go on for one clinic after its circuit breaker stopped it: until when, who, and why.';
    END IF;
END $$;

COMMIT;
