-- ================================================================
-- 048 - what the PC says it still holds of each recording (20 Nov 2026)
--
-- From recorder 3.9.6 every heartbeat lists, for each recording the PC
-- still holds something of, how many pieces it has sealed, how many the
-- server accepted, and the numbers still waiting to be sent or refused.
-- Kept on the recording, so the pieces log can say "pieces 16-20 are still
-- on DESKTOP-0D1PA7I" instead of only "this PC has 5 pieces waiting".
--
-- Older recorders send nothing and these stay empty.
-- Adds only; nothing is deleted or changed. Safe to run twice.
-- ================================================================
BEGIN;

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pc_pieces_recorded INTEGER;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pc_pieces_sent INTEGER;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pc_pieces_waiting INTEGER;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pc_waiting_numbers INTEGER[];
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pc_rejected_numbers INTEGER[];
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pc_finished_recording BOOLEAN;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS pc_reported_at TIMESTAMP WITH TIME ZONE;

COMMENT ON COLUMN sessions.pc_pieces_recorded IS
    'Recorder 3.9.6+: pieces the PC has sealed for this recording, at its last report.';
COMMENT ON COLUMN sessions.pc_pieces_sent IS
    'Recorder 3.9.6+: pieces of this recording the server accepted, as the PC counts them.';
COMMENT ON COLUMN sessions.pc_pieces_waiting IS
    'Recorder 3.9.6+: pieces still on the PC waiting to be sent; 0 once all are sent.';
COMMENT ON COLUMN sessions.pc_waiting_numbers IS
    'Recorder 3.9.6+: the numbers of the pieces still waiting on the PC.';
COMMENT ON COLUMN sessions.pc_rejected_numbers IS
    'Recorder 3.9.6+: pieces the server refused, kept on the PC for review.';
COMMENT ON COLUMN sessions.pc_finished_recording IS
    'Recorder 3.9.6+: the PC has ended this recording (it may still be sending).';
COMMENT ON COLUMN sessions.pc_reported_at IS
    'When the PC last reported this recording''s pieces.';

CREATE INDEX IF NOT EXISTS idx_sessions_pc_waiting
    ON sessions(device_id) WHERE pc_pieces_waiting > 0;

COMMIT;
