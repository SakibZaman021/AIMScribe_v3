-- ================================================================
-- 033 - CMED connections: trusted EHR addresses, evidence-based blocking
--
--   cmed_trusted_origins        the web addresses of CMED's EHR that may drive
--                               a recorder. Kept here, managed on the dashboard,
--                               sent to every recorder signed: no installer is
--                               told them, so one installer serves every clinic.
--                               A removed address keeps its row (removed_at).
--
--   devices.blocked_at          when the sweep blocked a self-registered PC on
--                               evidence: CMED's server was demonstrably working
--                               for its clinic, yet confirmed none of its
--                               recordings. An outage never blocks: with no word
--                               from CMED, there is no evidence either way.
--   devices.trust_judged_from   only recordings from this moment are judged;
--                               set when an admin unblocks a PC, so it is judged
--                               afresh rather than blocked again at once.
--
--   hospitals.cmed_silence_alerted_at
--                               the last "CMED has gone quiet while this clinic
--                               records" alert, so it is raised once, not every
--                               sweep.
--
-- Safe to run twice. Run as the tables' owner.
-- ================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS cmed_trusted_origins (
    origin      TEXT PRIMARY KEY
                CHECK (origin ~ '^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$'),
    added_by    TEXT NOT NULL,
    added_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    note        TEXT NOT NULL DEFAULT '',
    removed_by  TEXT,
    removed_at  TIMESTAMP WITH TIME ZONE
);
COMMENT ON TABLE cmed_trusted_origins IS
    'CMED''s EHR web addresses that may drive a recorder; sent to every recorder signed.';

ALTER TABLE devices ADD COLUMN IF NOT EXISTS blocked_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS trust_judged_from TIMESTAMP WITH TIME ZONE;
ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS cmed_silence_alerted_at TIMESTAMP WITH TIME ZONE;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_api') THEN
        GRANT SELECT, INSERT, UPDATE ON cmed_trusted_origins TO aims_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON cmed_trusted_origins TO aims_monitor;
    END IF;
END $$;

COMMIT;
