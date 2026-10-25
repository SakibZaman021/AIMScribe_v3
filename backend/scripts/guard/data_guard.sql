-- ================================================================
-- The data guard: what no update, mistake or stray command may do to the
-- data, enforced by PostgreSQL itself (applied by migrate.sh, as the
-- superuser, to both databases, on every update).
--
--   1. No table, column, schema or materialized view is dropped.
--   2. No table is truncated.
--   3. No single statement deletes more than 5,000 rows of a table.
--
-- The server's own logins already cannot do any of the first two (018: rows,
-- not structure); this stops the migrations - every update - and a person at
-- a psql prompt. Erasing a consultation deletes a few hundred rows at most;
-- the clean-ups of expired tokens and nonces delete 500 at a time.
--
-- A change that really must destroy data is decided in review: its migration
-- is listed in DESTRUCTIVE_APPROVED (deploy/uiu/tests/test_deployment.py,
-- which the deploy gate runs) and sets, for itself only,
--
--     SET aims.allow_destructive = 'on';
--
-- For truncating and deleting, the setting counts only for the table's owner
-- or a superuser: the server's login, or anything that gets into it, cannot
-- switch the guard off by setting it.
--
-- This protects against mistakes. Against someone holding the superuser's
-- password nothing inside the database is enough; that is what the off-site
-- copies under a bucket lock are for (DISASTER_RECOVERY.md).
--
-- The guard lives in its own schema, owned by the superuser: dropping
-- public with CASCADE must not take the guard with it.
-- ================================================================

CREATE SCHEMA IF NOT EXISTS aims_guard;
REVOKE CREATE ON SCHEMA aims_guard FROM PUBLIC;
GRANT USAGE ON SCHEMA aims_guard TO PUBLIC;

CREATE OR REPLACE FUNCTION aims_guard.approved() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT coalesce(current_setting('aims.allow_destructive', true), '') = 'on'
$$;

CREATE OR REPLACE FUNCTION aims_guard.owner_approved(rel oid) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT aims_guard.approved()
       AND (coalesce((SELECT rolsuper FROM pg_roles WHERE rolname = current_user), false)
            OR pg_has_role(current_user, (SELECT relowner FROM pg_class WHERE oid = rel),
                           'MEMBER'))
$$;

-- 1. Drops. Only an owner or a superuser can drop at all, so the setting is
-- enough here.
CREATE OR REPLACE FUNCTION aims_guard.on_drop() RETURNS event_trigger
LANGUAGE plpgsql AS $$
DECLARE
    gone record;
BEGIN
    IF aims_guard.approved() THEN
        RETURN;
    END IF;
    FOR gone IN SELECT * FROM pg_event_trigger_dropped_objects() LOOP
        IF NOT gone.is_temporary
           AND gone.object_type IN ('table', 'table column', 'schema', 'materialized view') THEN
            RAISE EXCEPTION 'data guard: % % would be dropped', gone.object_type,
                gone.object_identity
                USING ERRCODE = 'insufficient_privilege',
                      HINT = 'Data is never dropped by an update. If this was decided in '
                             'review, the migration sets aims.allow_destructive and is listed '
                             'in DESTRUCTIVE_APPROVED.';
        END IF;
    END LOOP;
END
$$;

-- 2. Truncating.
CREATE OR REPLACE FUNCTION aims_guard.on_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT aims_guard.owner_approved(TG_RELID) THEN
        RAISE EXCEPTION 'data guard: TRUNCATE %.% refused', TG_TABLE_SCHEMA, TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege',
                  HINT = 'A table is never emptied by an update or by hand.';
    END IF;
    RETURN NULL;
END
$$;

-- 3. Deleting many rows in one statement: a DELETE whose WHERE went wrong.
CREATE OR REPLACE FUNCTION aims_guard.on_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    deleted bigint;
BEGIN
    SELECT count(*) INTO deleted FROM aims_gone;
    IF deleted > 5000 AND NOT aims_guard.owner_approved(TG_RELID) THEN
        RAISE EXCEPTION 'data guard: one statement would delete % rows of %.%', deleted,
            TG_TABLE_SCHEMA, TG_TABLE_NAME
            USING ERRCODE = 'insufficient_privilege',
                  HINT = 'More than 5,000 rows at once is a mistake, not an erasure.';
    END IF;
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION aims_guard.install(rel regclass) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    -- OR REPLACE (PostgreSQL 14+): applied again on every update without
    -- ever dropping a guard trigger, which the guard itself refuses.
    EXECUTE format('CREATE OR REPLACE TRIGGER aims_guard_truncate BEFORE TRUNCATE ON %s '
                   'FOR EACH STATEMENT EXECUTE FUNCTION aims_guard.on_truncate()', rel);
    EXECUTE format('CREATE OR REPLACE TRIGGER aims_guard_delete AFTER DELETE ON %s '
                   'REFERENCING OLD TABLE AS aims_gone '
                   'FOR EACH STATEMENT EXECUTE FUNCTION aims_guard.on_delete()', rel);
END
$$;

-- A table a later migration creates is guarded the moment it exists.
CREATE OR REPLACE FUNCTION aims_guard.on_new_table() RETURNS event_trigger
LANGUAGE plpgsql AS $$
DECLARE
    made record;
BEGIN
    FOR made IN SELECT * FROM pg_event_trigger_ddl_commands()
                 WHERE command_tag IN ('CREATE TABLE', 'CREATE TABLE AS')
                   AND object_type = 'table' AND schema_name = 'public' LOOP
        PERFORM aims_guard.install(made.objid::regclass);
    END LOOP;
END
$$;

-- And the guard itself: its schema and functions are not dropped either.
-- Checked before the command runs, while the guard is still all there.
CREATE OR REPLACE FUNCTION aims_guard.on_drop_start() RETURNS event_trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT aims_guard.approved() AND current_query() ~* 'aims_guard' THEN
        RAISE EXCEPTION 'data guard: the guard itself is not dropped (%)', tg_tag
            USING ERRCODE = 'insufficient_privilege';
    END IF;
END
$$;

DROP EVENT TRIGGER IF EXISTS aims_guard_keep;
CREATE EVENT TRIGGER aims_guard_keep ON ddl_command_start
    WHEN TAG IN ('DROP SCHEMA', 'DROP FUNCTION', 'DROP TRIGGER', 'ALTER TABLE')
    EXECUTE FUNCTION aims_guard.on_drop_start();

DROP EVENT TRIGGER IF EXISTS aims_guard_drop;
CREATE EVENT TRIGGER aims_guard_drop ON sql_drop
    EXECUTE FUNCTION aims_guard.on_drop();

DROP EVENT TRIGGER IF EXISTS aims_guard_new_table;
CREATE EVENT TRIGGER aims_guard_new_table ON ddl_command_end
    WHEN TAG IN ('CREATE TABLE', 'CREATE TABLE AS')
    EXECUTE FUNCTION aims_guard.on_new_table();

-- Every table there is now.
DO $$
DECLARE
    t record;
BEGIN
    FOR t IN SELECT c.oid::regclass AS rel
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'public' AND c.relkind = 'r' LOOP
        PERFORM aims_guard.install(t.rel);
    END LOOP;
END
$$;
