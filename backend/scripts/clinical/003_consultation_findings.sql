-- ================================================================
-- aims_clinical 003 - the doctor's findings, with the prescription
--
--   consultation_findings   one row per finding the doctor recorded, part of
--                           the prescription (API 3) it came with:
--     section   chief_complaint | history | allergy | on_examination |
--               systemic_examination | gynae_obstetric
--     item      what it is about: the complaint itself; family / personal /
--               past / drug history; food / medicine / pollen / dust / mites;
--               anaemia, jaundice, dehydration, oedema, cyanosis, lymph
--               nodes; heart, lungs, abdomen; gynaecological, obstetric ...
--     value     what the doctor wrote
--     detail    the rest, as sent (a complaint's severity and duration)
--
-- API 2 carries what the paramedic collects when the patient is opened; the
-- doctor's findings come with the prescription, in API 3, and a corrected
-- prescription brings its findings again as the next version.
--
-- Run as aims_clinical_owner (deploy/uiu/postgres/migrate.sh). Safe to run twice.
-- ================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS consultation_findings (
    prescription_id BIGINT NOT NULL REFERENCES prescriptions ON DELETE CASCADE,
    line_no         INTEGER NOT NULL CHECK (line_no > 0),
    section         VARCHAR(32) NOT NULL CHECK (section IN (
                        'chief_complaint', 'history', 'allergy', 'on_examination',
                        'systemic_examination', 'gynae_obstetric')),
    item            TEXT NOT NULL CHECK (btrim(item) <> ''),
    value           TEXT,
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (prescription_id, line_no)
);
CREATE INDEX IF NOT EXISTS idx_consultation_findings_section
    ON consultation_findings (section, item);

COMMENT ON TABLE consultation_findings IS
    'The doctor''s findings with each prescription (API 3): chief complaints, history, allergies, on-examination and systemic examination, gynaecological and obstetric history. One row per finding.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_clinical_writer') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON consultation_findings TO aims_clinical_writer;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_clinical_reader') THEN
        GRANT SELECT ON consultation_findings TO aims_clinical_reader;
    END IF;
END $$;

COMMIT;
