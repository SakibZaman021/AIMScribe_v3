-- ================================================================
-- Researchers' own database logins - the recordings database, and the
-- roles themselves (roles belong to the whole server, not one database)
--
--   aims_researchers   the group every researcher's login belongs to:
--                      SELECT on the tables, nothing else
--   aims_role_keeper   owns the two functions below and can do only what
--                      they do: make, re-key and remove logins named r_...,
--                      each a member of aims_researchers. It cannot log in
--   r_<name>_<hash>    one login per person, made from the portal's People
--                      panel (backend/src/portal.py). Read-only, at most 3
--                      connections, every statement logged with its name
--
-- The server's own login (aims_api) may call the two functions and nothing
-- more: it never holds the right to create roles itself. A password never
-- reaches the database in the clear - the server sends its SCRAM-SHA-256
-- verifier, as `ALTER ROLE ... PASSWORD 'SCRAM-SHA-256$...'` allows.
--
-- Run by deploy/uiu/postgres/migrate.sh as the superuser on every update,
-- in aims_recordings. Safe to run again and again.
-- ================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_researchers') THEN
        CREATE ROLE aims_researchers NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_role_keeper') THEN
        CREATE ROLE aims_role_keeper NOLOGIN CREATEROLE;
    END IF;
END $$;

GRANT aims_researchers TO aims_role_keeper WITH ADMIN OPTION;
-- Each researcher's statements are logged under their own name.
GRANT SET ON PARAMETER log_statement TO aims_role_keeper;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO aims_researchers', current_database());
END $$;
GRANT USAGE ON SCHEMA public TO aims_researchers;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aims_researchers;
-- Nothing about sign-ins in progress or live portal sessions.
DO $$
BEGIN
    IF to_regclass('public.portal_logins') IS NOT NULL THEN
        REVOKE ALL ON portal_logins FROM aims_researchers;
    END IF;
    IF to_regclass('public.portal_sessions') IS NOT NULL THEN
        REVOKE ALL ON portal_sessions FROM aims_researchers;
    END IF;
END $$;


CREATE OR REPLACE FUNCTION portal_db_login_set(p_role text, p_verifier text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    found_role pg_roles%ROWTYPE;
BEGIN
    IF p_role IS NULL OR p_role !~ '^r_[a-z0-9_]{1,40}$' THEN
        RAISE EXCEPTION 'not a researcher login name: %', p_role;
    END IF;
    IF p_verifier IS NULL
       OR p_verifier !~ '^SCRAM-SHA-256\$[0-9]+:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$' THEN
        RAISE EXCEPTION 'a SCRAM-SHA-256 verifier is required, never a password';
    END IF;
    SELECT * INTO found_role FROM pg_roles WHERE rolname = p_role;
    IF FOUND THEN
        -- Only a login this function made: a member of aims_researchers and
        -- nothing more powerful.
        IF found_role.rolsuper OR found_role.rolcreaterole OR found_role.rolcreatedb
           OR found_role.rolreplication OR found_role.rolbypassrls
           OR NOT EXISTS (SELECT 1 FROM pg_auth_members m
                          WHERE m.member = found_role.oid
                            AND m.roleid = 'aims_researchers'::regrole) THEN
            RAISE EXCEPTION '% is not a researcher login', p_role;
        END IF;
        EXECUTE format('ALTER ROLE %I WITH LOGIN PASSWORD %L', p_role, p_verifier);
    ELSE
        EXECUTE format('CREATE ROLE %I LOGIN INHERIT NOCREATEDB NOCREATEROLE '
                       'CONNECTION LIMIT 3 PASSWORD %L IN ROLE aims_researchers',
                       p_role, p_verifier);
    END IF;
    EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = on', p_role);
    EXECUTE format('ALTER ROLE %I SET statement_timeout = %L', p_role, '120s');
    EXECUTE format('ALTER ROLE %I SET log_statement = %L', p_role, 'all');
END $$;


CREATE OR REPLACE FUNCTION portal_db_login_drop(p_role text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    IF p_role IS NULL OR p_role !~ '^r_[a-z0-9_]{1,40}$' THEN
        RAISE EXCEPTION 'not a researcher login name: %', p_role;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles r JOIN pg_auth_members m ON m.member = r.oid
                   WHERE r.rolname = p_role AND m.roleid = 'aims_researchers'::regrole
                     AND NOT r.rolsuper) THEN
        RETURN;                                   -- nothing to remove
    END IF;
    -- No new connection from this moment; then the login goes altogether.
    EXECUTE format('ALTER ROLE %I WITH NOLOGIN PASSWORD NULL', p_role);
    BEGIN
        EXECUTE format('DROP ROLE %I', p_role);
    EXCEPTION WHEN dependent_objects_still_exist OR object_in_use THEN
        NULL;                                     -- left unable to log in
    END;
END $$;

ALTER FUNCTION portal_db_login_set(text, text) OWNER TO aims_role_keeper;
ALTER FUNCTION portal_db_login_drop(text) OWNER TO aims_role_keeper;
REVOKE ALL ON FUNCTION portal_db_login_set(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION portal_db_login_drop(text) FROM PUBLIC;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_api') THEN
        GRANT EXECUTE ON FUNCTION portal_db_login_set(text, text) TO aims_api;
        GRANT EXECUTE ON FUNCTION portal_db_login_drop(text) TO aims_api;
    END IF;
END $$;
