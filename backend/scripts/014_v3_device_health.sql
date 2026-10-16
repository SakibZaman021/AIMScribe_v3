-- ================================================================
-- 014 - What a recorder saw while it could not reach this server.
--
-- A recorder that loses the network keeps recording, and keeps a small
-- health log: a line a minute while offline (alive, recording or idle,
-- microphone level) and every alert it raised. When the network is back
-- the log is sent here, in order, and the recorder deletes its copy only
-- after this server says it is stored. The dashboard reads offline
-- periods and late alerts from it, instead of a gap.
--
-- Additive only.
-- ================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS device_health_log (
    device_id    UUID NOT NULL REFERENCES devices(device_id),
    seq          BIGINT NOT NULL,              -- the recorder's own order
    kind         VARCHAR(16) NOT NULL CHECK (kind IN ('heartbeat', 'alert')),
    occurred_at  TIMESTAMP WITH TIME ZONE NOT NULL,
    data         JSONB NOT NULL DEFAULT '{}',
    received_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, seq)              -- a resent batch stores nothing twice
);
CREATE INDEX IF NOT EXISTS idx_device_health_recent
    ON device_health_log (device_id, occurred_at DESC);

COMMIT;
