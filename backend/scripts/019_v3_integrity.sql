-- ================================================================
-- 019 - what the data-safety review asked of the database
--
--   devices.signs_requests   a PC that has signed one request must sign
--                            them all; a copied token alone is then useless
--   device_nonces            each signed request's nonce, kept for the
--                            15-minute window, so none can be replayed
--   session_refusals.signed  whether the refusal carried the PC's signature
--   sessions.late_alerted_at the missing-clips alert, raised once per
--                            recording (it was a set in one process's memory)
--   erase_tasks              files and cloud copies an erased recording left;
--                            the archive worker and this server remove them
--   uq_chain_genesis_hash    one genesis entry opens one recording: the
--                            genesis payload names no session id, so without
--                            this a replayed genesis could open another one
--                            and carry that recording's signed entries
-- ================================================================

ALTER TABLE devices ADD COLUMN IF NOT EXISTS signs_requests BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS device_nonces (
    device_id  UUID NOT NULL,
    nonce      VARCHAR(64) NOT NULL,
    seen_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, nonce)
);
CREATE INDEX IF NOT EXISTS idx_device_nonces_seen ON device_nonces (seen_at);

ALTER TABLE session_refusals ADD COLUMN IF NOT EXISTS signed BOOLEAN;
CREATE INDEX IF NOT EXISTS idx_refusals_device_time ON session_refusals (device_id, refused_at);

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS late_alerted_at TIMESTAMP WITH TIME ZONE;

CREATE TABLE IF NOT EXISTS erase_tasks (
    session_id    VARCHAR(64) PRIMARY KEY,
    files         JSONB NOT NULL DEFAULT '[]'::jsonb,    -- archive paths to remove
    cloud_keys    JSONB NOT NULL DEFAULT '[]'::jsonb,    -- [{kind, object_key}] still to delete
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    completed_at  TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_erase_tasks_open ON erase_tasks (created_at)
    WHERE completed_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_chain_genesis_hash
    ON chain_entries (entry_hash) WHERE entry_no = 0;

-- The server's own login (018) may use the new tables. 018's default
-- privileges cover this when 019 is run by the same role; this covers a run
-- by the superuser.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_api') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON device_nonces, erase_tasks TO aims_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON device_nonces, erase_tasks TO aims_monitor;
    END IF;
END $$;
