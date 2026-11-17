-- ================================================================
-- Researchers' own database logins - the clinical database
--
-- The same group as in the recordings database (researcher_logins_
-- recordings.sql makes it): SELECT on every table, nothing else. Run by
-- deploy/uiu/postgres/migrate.sh as the superuser on every update, in
-- aims_clinical, after the recordings file. Safe to run again and again.
-- ================================================================

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_researchers') THEN
        EXECUTE format('GRANT CONNECT ON DATABASE %I TO aims_researchers', current_database());
        GRANT USAGE ON SCHEMA public TO aims_researchers;
        GRANT SELECT ON ALL TABLES IN SCHEMA public TO aims_researchers;
    END IF;
END $$;

-- 18 Nov 2026: whoever opens this database to read it starts in by_visit -
-- the visit and patient tables a person can read (clinical/007, 008) -
-- instead of public's internal tables keyed by numbers. Only the reading
-- logins: the read-only viewer login and every researcher login. The
-- server's own login (aims_clinical_writer) keeps public, so nothing it
-- does changes. public stays on the path: every earlier query still works.
DO $$
DECLARE
    who TEXT;
BEGIN
    IF to_regnamespace('by_visit') IS NULL THEN
        RETURN;
    END IF;
    FOR who IN
        SELECT r.rolname FROM pg_roles r
         WHERE r.rolname = 'aims_clinical_reader'
            OR (r.rolname ~ '^r_[a-z0-9_]{1,40}$' AND EXISTS (
                    SELECT 1 FROM pg_auth_members m
                     WHERE m.member = r.oid AND m.roleid = 'aims_researchers'::regrole))
    LOOP
        EXECUTE format('ALTER ROLE %I IN DATABASE %I SET search_path = by_visit, public',
                       who, current_database());
    END LOOP;
END $$;
