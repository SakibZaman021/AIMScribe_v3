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
