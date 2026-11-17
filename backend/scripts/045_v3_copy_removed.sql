-- ================================================================
-- 045 - a cloud copy that was removed keeps its row, marked
--
-- After 044 every copy also lives in its visit's folder. On 17 Nov 2026 the
-- user decided to remove the old by-day copies (copies/<clinic>/<doctor>/
-- <date>/), each only once its twin in visits/<patient>/<visit_key>/ is
-- confirmed in the store with the same size and fingerprint
-- (visit_layout.retire_old_copies). The row stays, as the record that the
-- copy existed and where it went; removed_at says when it left the store,
-- and nothing reads a removed copy for a download.
--
-- Additions only: one column. Safe to run twice. Run as the tables' owner.
-- ================================================================
BEGIN;

ALTER TABLE cloud_copies ADD COLUMN IF NOT EXISTS removed_at TIMESTAMP WITH TIME ZONE;

COMMENT ON COLUMN cloud_copies.removed_at IS
    'When this object was removed from the store (an old by-day copy whose visit-folder twin holds the same bytes); NULL while it is there.';

COMMIT;
