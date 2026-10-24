-- ================================================================
-- 029 - downloading many recordings at once, in numbered batches
--
--   portal_batches       one row per batch a person asked for: the
--                        selection (clinic, dates, doctors), its number in
--                        that selection ("batch 2: files 201-400 of 600"),
--                        and how its download went
--   portal_batch_files   the files in each batch, in order, and whether
--                        each was sent whole
--
-- A file counts as downloaded by a person once a batch of theirs that held
-- it finished and the file in it was whole. The next batch of the same
-- selection is chosen from what is left, so batches never repeat a file and
-- never skip one - whatever was downloaded before, however it was chosen.
-- A batch that broke off is "failed" and its files are left for the next.
--
-- Written by the server (backend/src/bulk.py); nobody edits it.
-- Safe to run twice. Run as the tables' owner.
-- ================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS portal_batches (
    id               BIGSERIAL PRIMARY KEY,
    email            TEXT NOT NULL REFERENCES portal_people (email),
    hospital_id      VARCHAR(64),                    -- NULL: every clinic
    date_from        DATE NOT NULL,
    date_to          DATE NOT NULL,
    doctors          TEXT[],                         -- NULL: every doctor
    selection_key    CHAR(64) NOT NULL,
    batch_no         INTEGER NOT NULL,
    first_no         INTEGER NOT NULL,               -- files first_no..last_no
    last_no          INTEGER NOT NULL,               --   of selection_total
    selection_total  INTEGER NOT NULL,
    files            INTEGER NOT NULL,
    bytes_estimate   BIGINT NOT NULL DEFAULT 0,
    created_at       TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    started_at       TIMESTAMP WITH TIME ZONE,
    progress_at      TIMESTAMP WITH TIME ZONE,
    finished_at      TIMESTAMP WITH TIME ZONE,
    attempts         INTEGER NOT NULL DEFAULT 0,
    files_done       INTEGER NOT NULL DEFAULT 0,
    files_failed     INTEGER NOT NULL DEFAULT 0,
    bytes_sent       BIGINT NOT NULL DEFAULT 0,
    outcome          VARCHAR(16) NOT NULL DEFAULT 'ready'
                     CHECK (outcome IN ('ready', 'started', 'completed', 'failed', 'abandoned')),
    reason           TEXT,
    address          TEXT
);
CREATE INDEX IF NOT EXISTS idx_portal_batches_email
    ON portal_batches (email, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_portal_batches_running
    ON portal_batches (progress_at) WHERE outcome = 'started';

CREATE TABLE IF NOT EXISTS portal_batch_files (
    batch_id    BIGINT NOT NULL REFERENCES portal_batches (id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    session_id  VARCHAR(64) NOT NULL,
    part_no     INTEGER NOT NULL DEFAULT 0,          -- 0: a recording not cut
    object_key  TEXT NOT NULL,
    outcome     VARCHAR(8) NOT NULL DEFAULT 'waiting'
                CHECK (outcome IN ('waiting', 'sent', 'failed')),
    PRIMARY KEY (batch_id, position)
);
CREATE INDEX IF NOT EXISTS idx_portal_batch_files_file
    ON portal_batch_files (session_id, part_no);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON portal_batches, portal_batch_files TO aims_monitor;
    END IF;
    IF to_regclass('public.portal_batches') IS NOT NULL
       AND pg_has_role(current_user, (SELECT relowner FROM pg_class
                                      WHERE oid = to_regclass('public.portal_batches')), 'USAGE') THEN
        COMMENT ON TABLE portal_batches IS
            'Bulk downloads from the portal: who asked for which selection, which numbered batch of it, and whether it all arrived.';
        COMMENT ON TABLE portal_batch_files IS
            'The files in each bulk download batch, in order, and whether each was sent whole.';
    END IF;
END $$;

COMMIT;
