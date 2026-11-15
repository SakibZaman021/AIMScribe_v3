-- ============================================================
-- 040: a data collector's official name and lab ID
--
-- Collectors sign in with Google, whose name is often a nickname. The lab
-- gives each collector an ID (DC-07); on My shift the collector saves their
-- official name and that ID, beside their phone (039), and an admin can
-- correct them in People. Researchers' alerts, the evening report and the
-- attendance CSV then name "Md. Sakib Zaman (DC-07)".
--
--   portal_people.official_name    the collector's official name
--   portal_people.collector_code   the lab's ID for them; two people still
--                                  in the lab never hold the same one
--
-- Additions only: two new columns and an index. Nothing is deleted.
-- ============================================================

BEGIN;

ALTER TABLE portal_people ADD COLUMN IF NOT EXISTS official_name VARCHAR(80);
ALTER TABLE portal_people ADD COLUMN IF NOT EXISTS collector_code VARCHAR(20);
CREATE UNIQUE INDEX IF NOT EXISTS uq_portal_people_collector_code
    ON portal_people (upper(collector_code))
    WHERE collector_code IS NOT NULL AND removed_at IS NULL;

COMMENT ON COLUMN portal_people.official_name IS 'A data collector''s official name, as the lab knows them.';
COMMENT ON COLUMN portal_people.collector_code IS 'The ID the lab gave this data collector (DC-07); unique among people still in the lab.';

COMMIT;
