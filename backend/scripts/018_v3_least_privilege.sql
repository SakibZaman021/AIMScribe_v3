-- ================================================================
-- 018: the API stops connecting as the owner of its tables.
--
-- Until now the server, the transcription worker and PgBouncer all logged in
-- as aims_recordings, which owns every table. An owner can do anything to
-- its own tables, including
--
--     ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_change;
--
-- so the append-only audit log was append-only for everyone except the one
-- account an attacker would actually hold after breaking into the API.
--
-- From here:
--   aims_recordings   owns the schema. NOLOGIN on a new server; migrations
--                     run as the superuser under SET ROLE aims_recordings.
--   aims_api          what the server and the transcription worker log in
--                     as. Reads and writes rows; cannot create, alter, drop
--                     or truncate anything, cannot touch a trigger, and can
--                     only add to and read the audit log.
--
-- Run as the owner (SET ROLE aims_recordings), like every migration. The
-- role itself is created by the superuser - by 01_databases.sh on a new
-- server, or by the upgrade step in the deployment runbook:
--
--     CREATE ROLE aims_api LOGIN PASSWORD '...';
--
-- No password is set here: this file is in a public repository.
-- Safe to run again.
-- ================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_api') THEN
        BEGIN
            -- Only a role with CREATEROLE gets this far (the test servers).
            CREATE ROLE aims_api LOGIN;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE EXCEPTION 'role aims_api does not exist. As the superuser, run: '
                'CREATE ROLE aims_api LOGIN PASSWORD ''<AIMS_API_DB_PASSWORD>''; '
                'then apply 018 again.';
        END;
    END IF;
END $$;

-- The application role must not be, or become, the owner by membership.
DO $$
DECLARE
    owner_name text;
BEGIN
    SELECT pg_get_userbyid(relowner) INTO owner_name
      FROM pg_class WHERE oid = to_regclass('public.audit_log');
    IF owner_name IS NOT NULL AND owner_name <> 'aims_api'
       AND pg_has_role('aims_api', owner_name, 'MEMBER') THEN
        EXECUTE format('REVOKE %I FROM aims_api', owner_name);
    END IF;
END $$;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO aims_api', current_database());
END $$;

-- Nobody else creates objects in the schema (PostgreSQL 15+ already says so;
-- 14 and earlier did not).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO aims_api;

-- Rows, not structure. TRUNCATE, REFERENCES and TRIGGER are deliberately left
-- out: TRUNCATE skips row triggers, and TRIGGER is how a table's are created.
-- Table by table, so a table this role may not grant on (one a superuser
-- created by hand) is reported instead of stopping the migration.
DO $$
DECLARE
    rel record;
    me_super boolean := (SELECT rolsuper FROM pg_roles WHERE rolname = current_user);
BEGIN
    FOR rel IN
        SELECT c.relname, c.relkind, pg_get_userbyid(c.relowner) AS owner
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
    LOOP
        IF NOT (me_super OR pg_has_role(current_user, rel.owner, 'USAGE')) THEN
            RAISE NOTICE 'not granted on %: owned by %; run 018 as the superuser',
                rel.relname, rel.owner;
        ELSIF rel.relkind = 'S' THEN
            EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE public.%I TO aims_api', rel.relname);
        ELSE
            IF rel.relkind = 'm' THEN
                EXECUTE format('GRANT SELECT ON public.%I TO aims_api', rel.relname);
            ELSE
                EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON public.%I TO aims_api',
                               rel.relname);
            END IF;
            -- The monitor keeps reading whatever exists.
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
                EXECUTE format('GRANT SELECT ON public.%I TO aims_monitor', rel.relname);
            END IF;
        END IF;
    END LOOP;
END $$;

-- The audit log is read and appended to, never changed (SRS-INT audit chain).
-- Its triggers refuse UPDATE and DELETE as well; this makes the refusal
-- independent of them, so disabling a trigger is no longer enough - and the
-- API cannot disable one anyway.
DO $$
BEGIN
    IF to_regclass('public.audit_log') IS NOT NULL THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM aims_api;
        GRANT SELECT, INSERT ON audit_log TO aims_api;
    END IF;
END $$;

-- Tables and sequences later migrations add get the same grants - created by
-- whoever runs this (the owner under SET ROLE, or a superuser applying it
-- directly) and by the owner of the schema's tables, when this role may act
-- for it. The audit-only rule above is per table; a future append-only table
-- must revoke the same way.
DO $$
DECLARE
    creator text;
BEGIN
    FOR creator IN
        SELECT DISTINCT r FROM (
            SELECT current_user::text AS r
            UNION
            SELECT pg_get_userbyid(relowner)::text FROM pg_class
             WHERE oid = to_regclass('public.audit_log')
        ) roles
         WHERE r <> 'aims_api'
           AND (r = current_user
                OR pg_has_role(current_user, r, 'MEMBER')
                OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user))
    LOOP
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                       'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aims_api', creator);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
                       'GRANT USAGE, SELECT ON SEQUENCES TO aims_api', creator);
    END LOOP;
END $$;

-- The monitor reads whatever later migrations add, too.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO aims_monitor;
    END IF;
END $$;
