-- ================================================================
-- 036 - Recorder updates: which AIMScribeSetup.exe each PC should run
--
--   recorder_releases   one row per release made available to recorders:
--                       its version, where to download it and the file's
--                       SHA-256 (from the GitHub Release the cloud build
--                       published), and how far it is rolled out:
--                         off        nobody
--                         test       the named test PCs (device id or name)
--                         hospitals  the test PCs and every PC of the named
--                                    hospitals
--                         all        every PC
--                       and whether that rollout is paused. The newest row is
--                       the one in force; an older release is offered again
--                       only as a new row marked rollback, which the server
--                       signs as such - a PC never installs an older version
--                       on any other say-so.
--
--   devices.update_status     what the PC's updater last reported (installed,
--                             waiting for the consultation to end, failed and
--                             why, gave up after three tries), as JSON.
--   devices.update_status_at  when it was reported.
--
-- Safe to run twice. Run as the tables' owner.
-- ================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS recorder_releases (
    release_id    BIGSERIAL PRIMARY KEY,
    version       VARCHAR(32) NOT NULL
                  CHECK (version ~ '^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$'),
    download_url  VARCHAR(500) NOT NULL CHECK (download_url ~ '^https://'),
    sha256        CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    rollback      BOOLEAN NOT NULL DEFAULT false,
    stage         VARCHAR(16) NOT NULL DEFAULT 'off'
                  CHECK (stage IN ('off', 'test', 'hospitals', 'all')),
    test_devices  TEXT[] NOT NULL DEFAULT '{}',
    hospitals     TEXT[] NOT NULL DEFAULT '{}',
    paused        BOOLEAN NOT NULL DEFAULT false,
    note          VARCHAR(300) NOT NULL DEFAULT '',
    created_by    VARCHAR(128) NOT NULL,
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    changed_by    VARCHAR(128),
    changed_at    TIMESTAMP WITH TIME ZONE
);
COMMENT ON TABLE recorder_releases IS
    'Recorder versions offered to PCs and how far each is rolled out; the newest row is in force.';

ALTER TABLE devices ADD COLUMN IF NOT EXISTS update_status JSONB;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS update_status_at TIMESTAMP WITH TIME ZONE;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_api') THEN
        GRANT SELECT, INSERT, UPDATE ON recorder_releases TO aims_api;
        GRANT USAGE, SELECT ON SEQUENCE recorder_releases_release_id_seq TO aims_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON recorder_releases TO aims_monitor;
    END IF;
END $$;

COMMIT;
