-- ================================================================
-- 052 - the real server takes only the clinics' own pages (26 Nov 2026)
--
-- Recorder 3.9.9 forwards, with each grant request, the EHR page that sent
-- API 1 (its Origin). The server refuses a page that is not on its own
-- trusted list, so a test page never records into the real server. The page
-- is kept on the grant:
--   grant_authorisations.origin
--
-- And the owner's rule for the real server: a PC that registers itself is
-- approved by an administrator on the dashboard, never by CMED's
-- confirmations alone. The switch, per server:
--   server_settings('auto_approve_pcs') = 'on' (default, beta) | 'off' (real)
--   UPDATE server_settings SET value = 'off', changed_at = now()
--    WHERE name = 'auto_approve_pcs';
--
-- Adds only; nothing is deleted. Safe to run twice.
-- ================================================================
BEGIN;

ALTER TABLE grant_authorisations ADD COLUMN IF NOT EXISTS origin TEXT;
COMMENT ON COLUMN grant_authorisations.origin IS
    'The EHR page that asked for this recording (recorder 3.9.9+); NULL from older recorders.';

CREATE TABLE IF NOT EXISTS server_settings (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    changed_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);
INSERT INTO server_settings (name, value) VALUES ('auto_approve_pcs', 'on')
    ON CONFLICT (name) DO NOTHING;

-- As 025, and only while auto_approve_pcs is 'on'.
CREATE OR REPLACE FUNCTION aims_approve_confirmed_device() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF COALESCE((SELECT value FROM server_settings WHERE name = 'auto_approve_pcs'), 'on')
       <> 'on' THEN
        RETURN NULL;
    END IF;
    IF NEW.confirmation = 'confirmed' AND NEW.device_id IS NOT NULL
       AND (TG_OP = 'INSERT' OR OLD.confirmation IS DISTINCT FROM 'confirmed') THEN
        UPDATE devices d
           SET approval = 'approved', approved_at = now(), approved_by = 'cmed',
               approval_audited = false
         WHERE d.device_id = NEW.device_id
           AND d.approval = 'pending'
           AND d.revoked_at IS NULL
           AND (SELECT count(*) FROM sessions s
                 WHERE s.device_id = NEW.device_id
                   AND s.confirmation = 'confirmed') >= 2;
    END IF;
    RETURN NULL;
END $$;

COMMIT;
