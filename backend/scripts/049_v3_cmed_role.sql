-- ================================================================
-- 049 - a CMED role on the portal (23 Nov 2026)
--
-- CMED staff see every hospital's recordings and logs and download the
-- clinical record (JSON) of any recording. They never download or play the
-- audio (one at a time or in batches), and have no admin tools, no database
-- viewer and no CSV downloads. Only the owner and admins may change people.
--
-- The one object touched is portal_people's role CHECK, widened to also
-- accept 'cmed'. Nothing is deleted or changed. Safe to run twice.
-- ================================================================
BEGIN;

DO $$
DECLARE
    c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
              WHERE conrelid = 'portal_people'::regclass AND contype = 'c'
                AND pg_get_constraintdef(oid) LIKE '%role%'
    LOOP
        EXECUTE format('ALTER TABLE portal_people DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;
ALTER TABLE portal_people ADD CONSTRAINT portal_people_role_check
    CHECK (role IN ('admin', 'cmed', 'researcher', 'collector'));

COMMIT;
