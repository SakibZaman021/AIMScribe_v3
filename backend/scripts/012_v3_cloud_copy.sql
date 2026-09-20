-- ================================================================
-- 012 - The cloud copy (SRS 3.2 §7.10, SRS-ARC-08..16).
--
-- Pieces are no longer deleted when a recording is archived. Each
-- finished consultation is first kept in the cloud as one merged,
-- losslessly compressed copy (FLAC) with its JSON beside it, both
-- encrypted at UIU before they leave. Only when that copy has been
-- uploaded, read back and recorded here are the pieces deleted.
--
-- Additive only.
-- ================================================================

BEGIN;

-- One row per object in the copy bucket. A visit whose prescription arrives
-- after archiving gets a new JSON object and a new row; earlier versions are
-- kept (SRS-ARC-13), so this is a history, not a status column.
CREATE TABLE IF NOT EXISTS cloud_copies (
    id           BIGSERIAL PRIMARY KEY,
    session_id   VARCHAR(64) NOT NULL,
    kind         VARCHAR(8) NOT NULL CHECK (kind IN ('audio', 'json')),
    object_key   TEXT NOT NULL UNIQUE,
    version      INTEGER NOT NULL DEFAULT 1,
    bytes        BIGINT NOT NULL,
    sha256       BYTEA NOT NULL,          -- the object as uploaded (encrypted)
    plain_sha256 BYTEA,                   -- the FLAC or JSON before encryption
    uploaded_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    verified_at  TIMESTAMP WITH TIME ZONE -- read back from the bucket and matched
);
CREATE INDEX IF NOT EXISTS idx_cloud_copies_session
    ON cloud_copies(session_id, kind, version DESC);

-- Where a recording stands after archiving.
--   copied_at           the cloud copy is complete, verified and recorded
--   segments_deleted_at the pieces have been removed from the segment bucket
--   json_stale          CMED sent a prescription after archiving; the JSON
--                       beside the audio has to be written again (SRS-ARC-13)
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS copied_at           TIMESTAMP WITH TIME ZONE;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS segments_deleted_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS json_stale          BOOLEAN NOT NULL DEFAULT false;

-- The worker's two queues: recordings archived but not yet copied, and
-- recordings whose JSON has to be written again.
CREATE INDEX IF NOT EXISTS idx_sessions_awaiting_copy
    ON sessions(archived_at) WHERE archived_at IS NOT NULL AND copied_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_json_stale
    ON sessions(archived_at) WHERE json_stale;

COMMIT;
