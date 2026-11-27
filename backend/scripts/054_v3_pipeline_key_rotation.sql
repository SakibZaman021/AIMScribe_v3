-- ================================================================
-- 054 - pipeline machines change their keys (27 Nov 2026)
--
-- A lab or metrics machine asks for new keys with a request signed by its
-- current key, and proves it holds the new one. Nothing changes until an
-- administrator approves; then the new keys replace the old, which stop
-- working at once. keys_since dates the keys in use, so a machine whose keys
-- are older than 90 days raises a reminder.
--
-- Adds only; nothing is deleted. Safe to run twice.
-- ================================================================
BEGIN;

ALTER TABLE pipeline_machines ADD COLUMN IF NOT EXISTS pending_sign_pubkey BYTEA;
ALTER TABLE pipeline_machines ADD COLUMN IF NOT EXISTS pending_box_pubkey  BYTEA;
ALTER TABLE pipeline_machines ADD COLUMN IF NOT EXISTS rotation_requested_at
    TIMESTAMP WITH TIME ZONE;
ALTER TABLE pipeline_machines ADD COLUMN IF NOT EXISTS keys_since
    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now();

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'pipeline_pending_keys_sized') THEN
        ALTER TABLE pipeline_machines ADD CONSTRAINT pipeline_pending_keys_sized CHECK (
            (pending_sign_pubkey IS NULL OR octet_length(pending_sign_pubkey) = 32) AND
            (pending_box_pubkey IS NULL OR octet_length(pending_box_pubkey) = 32));
    END IF;
END $$;

COMMENT ON COLUMN pipeline_machines.pending_sign_pubkey IS
    'New signing key a machine asked for (054); used only after an administrator approves.';
COMMENT ON COLUMN pipeline_machines.keys_since IS
    'When the keys in use were enrolled or last changed; a reminder after 90 days.';

COMMIT;
