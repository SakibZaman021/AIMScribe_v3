-- ================================================================
-- 032 - which microphone each PC records with
--
--   devices.microphone           the microphone the PC records with now: the
--                                one its open recording uses, else Windows'
--                                default (from 3.8's heartbeat, every 30 s)
--   devices.microphone_is_clinic true when that is the clinic's speakerphone
--                                (the Jabra), false when the PC fell back to
--                                another - its own microphone
--   sessions.microphones         every microphone the recording used, in the
--                                order first heard: one name normally, two
--                                when the Jabra was unplugged mid-consultation
--
-- Columns only, all nullable: PCs on older recorders report nothing.
-- Safe to run twice. Run as the tables' owner.
-- ================================================================
BEGIN;

ALTER TABLE devices ADD COLUMN IF NOT EXISTS microphone TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS microphone_is_clinic BOOLEAN;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS microphones TEXT[];

COMMENT ON COLUMN devices.microphone IS
    'The microphone this PC records with now (recorder 3.8+, every heartbeat).';
COMMENT ON COLUMN devices.microphone_is_clinic IS
    'True when that is the clinic speakerphone (Jabra); false when it fell back to another.';
COMMENT ON COLUMN sessions.microphones IS
    'Every microphone this recording used, in the order first heard.';

COMMIT;
