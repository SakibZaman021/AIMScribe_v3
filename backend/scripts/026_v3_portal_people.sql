-- ================================================================
-- 026 - who may open the portal (the dashboard and the database viewer)
--
--   portal_people     everyone allowed in, by Google email, with a role:
--                     admin (everything, and managing people) or
--                     researcher (look, never change)
--   portal_sessions   a signed-in browser: the SHA-256 of its cookie, the
--                     person, when it ends. Removing a person ends their
--                     sessions at once
--   portal_logins     a sign-in in progress (Google's state, nonce and the
--                     PKCE verifier), for ten minutes at most
--
-- The owner's email (AIMS_PORTAL_OWNER_EMAIL) is always an admin; the
-- server writes that row on start, so the portal can never lock its owner out.
--
-- Safe to run twice. Run as the tables' owner.
-- ================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS portal_people (
    email          TEXT PRIMARY KEY CHECK (email = lower(email) AND position('@' in email) > 1),
    role           VARCHAR(16) NOT NULL CHECK (role IN ('admin', 'researcher')),
    display_name   TEXT,
    added_by       TEXT NOT NULL,
    added_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    last_signed_in TIMESTAMP WITH TIME ZONE,
    removed_at     TIMESTAMP WITH TIME ZONE,
    removed_by     TEXT
);

CREATE TABLE IF NOT EXISTS portal_sessions (
    token_sha256 BYTEA PRIMARY KEY CHECK (octet_length(token_sha256) = 32),
    email        TEXT NOT NULL REFERENCES portal_people (email),
    created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    expires_at   TIMESTAMP WITH TIME ZONE NOT NULL,
    ended_at     TIMESTAMP WITH TIME ZONE,
    address      TEXT,
    user_agent   TEXT
);
CREATE INDEX IF NOT EXISTS idx_portal_sessions_email ON portal_sessions (email) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS portal_logins (
    state        TEXT PRIMARY KEY,
    nonce        TEXT NOT NULL,
    verifier     TEXT NOT NULL,
    next_path    TEXT NOT NULL DEFAULT '/dashboard',
    created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON portal_people TO aims_monitor;
    END IF;
END $$;

-- The read-only logins can see these tables (the migration runner grants
-- them every table); a session row holds only a SHA-256 of a random
-- 32-byte cookie, which cannot be turned back into the cookie.

DO $$
DECLARE
    item text[];
BEGIN
    FOREACH item SLICE 1 IN ARRAY ARRAY[
        ['portal_people',   'Who may open the dashboard and the database viewer: Google email, role (admin or researcher), who added them.'],
        ['portal_sessions', 'Signed-in browsers: the hash of the cookie only, the person, when it ends.'],
        ['portal_logins',   'Google sign-ins in progress, for ten minutes at most.']
    ] LOOP
        IF to_regclass('public.' || item[1]) IS NOT NULL
           AND pg_has_role(current_user, (SELECT relowner FROM pg_class
                                          WHERE oid = to_regclass('public.' || item[1])), 'USAGE') THEN
            EXECUTE format('COMMENT ON TABLE public.%I IS %L', item[1], item[2]);
        END IF;
    END LOOP;
END $$;

COMMIT;
