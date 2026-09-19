-- ================================================================
-- Roles for aims_clinical (SRS-DBA-15, SRS-DBA-20).
--
-- Separate read, write and owner roles; no application connects as the
-- owner. Passwords are NOT set here - this file is in a public repository.
-- An administrator sets them once, by hand, on the server:
--
--     ALTER ROLE aims_clinical_writer PASSWORD '...';
--     ALTER ROLE aims_clinical_reader PASSWORD '...';
--
-- The recordings service's credentials are never granted anything here,
-- so a role that can search recordings cannot read prescriptions
-- (SRS-DAT-16, SRS-DBA-22b).
-- ================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_clinical_owner') THEN
        CREATE ROLE aims_clinical_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_clinical_writer') THEN
        CREATE ROLE aims_clinical_writer LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_clinical_reader') THEN
        CREATE ROLE aims_clinical_reader LOGIN;
    END IF;
END $$;

-- The Channel B service: writes what CMED sends, reads to load and link.
GRANT CONNECT ON DATABASE aims_clinical TO aims_clinical_writer, aims_clinical_reader;
GRANT USAGE ON SCHEMA public TO aims_clinical_writer, aims_clinical_reader;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    intake_records, intake_quarantine, patients, encounters, encounter_demographics,
    paramedic_observations, female_details, male_details, prescriptions,
    prescription_items, diagnoses, investigations
    TO aims_clinical_writer;
GRANT INSERT, SELECT ON clinical_access_log TO aims_clinical_writer;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO aims_clinical_writer;

-- The named clinical group (OD-19): read-only, and every read is logged by
-- the application in clinical_access_log.
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aims_clinical_reader;
GRANT INSERT ON clinical_access_log TO aims_clinical_reader;
GRANT USAGE ON SEQUENCE clinical_access_log_id_seq TO aims_clinical_reader;
