-- ============================================================
-- 041: the lab's shift times - 08:00-14:00 and 14:30-20:30
--
-- Places were made with an evening shift of 14:00-20:00, the first guess.
-- The lab's shifts are 08:00-14:00 and 14:30-20:30 (16 Nov 2026): the
-- evening summary goes at 20:30, and after it nobody is messaged until
-- the morning shift. A place still on the first guess moves to 14:30-20:30;
-- a place whose times an admin changed keeps them. New places start on
-- the lab's times.
--
-- No column or row is removed.
-- ============================================================

BEGIN;

ALTER TABLE places ALTER COLUMN evening_start SET DEFAULT '14:30';
ALTER TABLE places ALTER COLUMN evening_end SET DEFAULT '20:30';

UPDATE places SET evening_start = '14:30', evening_end = '20:30'
 WHERE evening_start = '14:00' AND evening_end = '20:00';

COMMENT ON COLUMN places.evening_end IS 'End of the evening shift (20:30 for the lab); the evening summary goes then, and quiet hours start.';

COMMIT;
