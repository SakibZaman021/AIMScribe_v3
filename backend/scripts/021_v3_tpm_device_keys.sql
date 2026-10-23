-- ================================================================
-- 021 - a PC's identity key may live in its TPM chip (recorder 3.6)
--
--   devices.tpm_pubkey   32 bytes: Ed25519, kept in software (up to 3.5)
--                        65 bytes: ECDSA P-256 as 0x04 || X || Y, created
--                        inside the PC's TPM, which cannot export it
--
-- The server tells the two apart by length (integrity.device_key_kind).
-- Safe to run twice. Run as the tables' owner.
-- ================================================================

ALTER TABLE devices DROP CONSTRAINT IF EXISTS devices_pubkey_len;
ALTER TABLE devices ADD CONSTRAINT devices_pubkey_len CHECK (
    octet_length(tpm_pubkey) = 32
    OR (octet_length(tpm_pubkey) = 65 AND get_byte(tpm_pubkey, 0) = 4)
);
