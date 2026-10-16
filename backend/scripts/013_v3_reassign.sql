-- ================================================================
-- 013 - A recording moved to the patient its prescription named.
--
-- The doctor opened the wrong profile first and corrected it; the
-- prescription was built for the right patient. The recorder signs a
-- 'reassign' entry into the chain and the server moves the recording.
-- The chain table has to accept the new entry type, or the entry - and
-- every entry after it - is refused and the recording never completes.
--
-- Additive only: every existing entry type is still accepted.
-- ================================================================

BEGIN;

ALTER TABLE chain_entries DROP CONSTRAINT IF EXISTS chain_entries_entry_type_check;
ALTER TABLE chain_entries ADD CONSTRAINT chain_entries_entry_type_check
    CHECK (entry_type IN ('open', 'segment', 'pause', 'resume', 'reassign', 'close'));

COMMIT;
