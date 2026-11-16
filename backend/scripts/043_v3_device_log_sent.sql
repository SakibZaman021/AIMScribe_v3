-- ============================================================
-- 043: the device log keeps what the PC sent
--
-- 042 kept what each PC was told. To pin down why, an admin also needs what
-- the PC sent: its headers (recorder version, whether a device token came and
-- which one by fingerprint, whether the request was signed, how far the PC's
-- clock is from the server's) and the fields of its request (machine name,
-- CMED code, protocol version, key kind...). device_events.sent holds them,
-- for the latest attempt of each line.
--
-- Never a secret or a patient value: a token, a key proof or a signature is
-- kept as "sent", its length and a short fingerprint; a patient identifier as
-- "sent" and its length. Admins only, as 042.
--
-- Additions only: one new column. Nothing is deleted.
-- ============================================================

BEGIN;

ALTER TABLE device_events ADD COLUMN IF NOT EXISTS sent JSONB;

COMMENT ON COLUMN device_events.sent IS 'What the PC sent, for the latest attempt: headers and request fields; secrets and patient values only as present/length/fingerprint.';

COMMIT;
