-- ================================================================
-- 028 - each person's own database login, by name
--
--   portal_people.db_user           the PostgreSQL login made for this
--                                   person (r_<name>_<hash>), or null
--   portal_people.db_password_set_at  when its password was last set
--
-- The logins themselves are made by portal_db_login_set (scripts/roles/
-- researcher_logins_recordings.sql, applied by the migration runner as the
-- superuser). The password is never stored here or anywhere: it is shown
-- once to the admin who set it.
--
-- Safe to run twice. Run as the tables' owner.
-- ================================================================

BEGIN;

ALTER TABLE portal_people ADD COLUMN IF NOT EXISTS db_user TEXT;
ALTER TABLE portal_people ADD COLUMN IF NOT EXISTS db_password_set_at TIMESTAMP WITH TIME ZONE;
CREATE UNIQUE INDEX IF NOT EXISTS idx_portal_people_db_user
    ON portal_people (db_user) WHERE db_user IS NOT NULL;

COMMIT;
