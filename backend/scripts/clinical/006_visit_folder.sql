-- ================================================================
-- clinical/006 - one visit, one folder: findable by patient
--
-- With thousands of patients, the tables (encounters, encounter_demographics,
-- patient_visit, visit_drug_order ...) had to be opened one after another to
-- see one patient's history. Each visit now has a key that is also its folder
-- in the cloud archive:
--
--     <patient_id>_<doctor_id>_<visit_id>          e.g. P4347460_DR4290371_VIS-T-31496
--     <patient_id>_<doctor_id>_<YYYYMMDD>T<HHMMSS> a visit CMED sent no visit_id for,
--                                                  from CMED's start_time (clinic time)
--
--   patient_visit.visit_key   generated from the visit's own columns
--   encounters.visit_key      the same, for visits that came without a visit_id
--   visit_objects             every file the cloud archive holds for a visit:
--                             audio and clinical JSON, each version, each part
--   v_visit_folder            one row per visit: who, when, demographics, vitals,
--                             complaints, diagnoses, drugs, tests, advice, the
--                             recordings and where their files are
--   patient_folder(id)        that view for one patient, newest visit first
--
--     SELECT * FROM patient_folder('P4347460');
--
-- The same key is computed in Python (api_v2.visit_key); test_visit_layout.py
-- holds the two to the same answer. Additions only: two generated columns,
-- one table, one view, one function. Nothing is changed or removed.
-- Run as the owner (SET ROLE aims_clinical_owner). Safe to repeat.
-- ================================================================

BEGIN;

-- The clinic-time stamp of CMED's start_time, as written
-- (2026-11-17T11:50:39+06:00 -> 20261117T115039). Text only: no time zone
-- database is needed, and the value is what CMED sent.
CREATE OR REPLACE FUNCTION visit_start_stamp(start_time TEXT) RETURNS TEXT
    LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
$$ SELECT replace(replace(left(start_time, 19), '-', ''), ':', '') $$;

ALTER TABLE patient_visit ADD COLUMN IF NOT EXISTS visit_key TEXT
    GENERATED ALWAYS AS (hospital_patient_id || '_' || COALESCE(practitioner_id, 'unknown')
                         || '_' || visit_id) STORED;
ALTER TABLE encounters ADD COLUMN IF NOT EXISTS visit_key TEXT
    GENERATED ALWAYS AS (patient_id || '_' || doctor_id || '_'
                         || visit_start_stamp(start_time)) STORED;

CREATE INDEX IF NOT EXISTS idx_patient_visit_visit_key ON patient_visit (visit_key);
CREATE INDEX IF NOT EXISTS idx_patient_visit_by_patient
    ON patient_visit (hospital_patient_id, visit_date DESC);
CREATE INDEX IF NOT EXISTS idx_encounters_visit_key ON encounters (visit_key);

-- Every file the cloud archive holds for a visit. Written by the server when a
-- copy is verified (and by scripts/backfill_visit_layout.py for copies made
-- before this). Never changed: a new version of a file is a new row.
CREATE TABLE IF NOT EXISTS visit_objects (
    id          BIGSERIAL PRIMARY KEY,
    visit_key   TEXT NOT NULL,
    patient_id  VARCHAR(64) NOT NULL,
    doctor_id   VARCHAR(64) NOT NULL,
    visit_id    VARCHAR(64),
    hospital_id VARCHAR(64),                     -- AIMS LAB's clinic
    session_id  VARCHAR(64) NOT NULL,            -- the recording
    part_no     INTEGER,                         -- one patient's part of a shared recording
    kind        VARCHAR(8) NOT NULL CHECK (kind IN ('audio', 'json')),
    object_key  TEXT NOT NULL UNIQUE,            -- in the cloud archive bucket
    version     INTEGER NOT NULL DEFAULT 1,
    bytes       BIGINT,
    stored_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_visit_objects_visit ON visit_objects (visit_key);
CREATE INDEX IF NOT EXISTS idx_visit_objects_patient ON visit_objects (patient_id);
CREATE INDEX IF NOT EXISTS idx_visit_objects_session ON visit_objects (session_id);

-- ---------------------------------------------------------------- the folder view
DROP FUNCTION IF EXISTS patient_folder(TEXT);
DROP VIEW IF EXISTS v_visit_folder;

CREATE VIEW v_visit_folder AS
WITH visits AS (
    -- Interface 4: keyed by CMED's visit_id.
    SELECT pv.visit_key, pv.hospital_patient_id AS patient_id,
           pv.practitioner_id AS doctor_id, pv.visit_id,
           pv.hospital_id AS cmed_hospital_id, pv.visit_date, pv.start_time,
           p.sex, p.date_of_birth, pv.age, pv.age_years, p.blood_group,
           pv.aims_id AS session_id, 'interface 4'::text AS source,
           pv.patient_information_message_id AS info_message,
           pv.prescription_message_id AS rx_message, NULL::uuid AS encounter_id,
           NULL::bigint AS prescription_id,
           pv.prescription_version, pv.follow_up, pv.follow_up_text
      FROM patient_visit pv
      JOIN patient p ON p.hospital_id = pv.hospital_id
                    AND p.hospital_patient_id = pv.hospital_patient_id
    UNION ALL
    -- Interfaces 2 and FHIR: a visit with no visit_id, unless interface 4
    -- holds the same visit (same clinic code, patient and start).
    SELECT e.visit_key, e.patient_id, e.doctor_id, NULL::varchar,
           e.cmed_hospital_id, e.visit_date, e.start_time::timestamptz,
           d.sex, d.date_of_birth, d.age_years::text, d.age_years::integer, NULL::text,
           e.session_id, 'interface 2'::text,
           NULL::bigint, NULL::bigint, e.encounter_id,
           lp.prescription_id, COALESCE(lp.version, 0), lp.follow_up_date, NULL::text
      FROM encounters e
      LEFT JOIN encounter_demographics d ON d.encounter_id = e.encounter_id
      LEFT JOIN latest_prescriptions lp ON lp.encounter_id = e.encounter_id
     WHERE e.source = 'live'
       AND NOT EXISTS (SELECT 1 FROM patient_visit pv
                        WHERE pv.hospital_id = e.cmed_hospital_id
                          AND pv.hospital_patient_id = e.patient_id
                          AND pv.start_time = e.start_time::timestamptz)
)
SELECT v.visit_key, v.patient_id, v.doctor_id, v.visit_id, v.cmed_hospital_id,
       v.visit_date, v.start_time, v.sex, v.date_of_birth, v.age, v.age_years,
       v.blood_group,
       CASE WHEN v.source = 'interface 4' THEN
           (SELECT string_agg(concat_ws(' ', x.name, x.value, x.unit), '; '
                              ORDER BY x.group_no, x.item_no)
              FROM visit_vital x WHERE x.message_id = v.info_message)
       ELSE
           (SELECT NULLIF(concat_ws('; ',
                       'BP ' || o.blood_pressure, 'Pulse ' || o.pulse_bpm || ' bpm',
                       'Temp ' || o.temperature_c || ' C', 'SpO2 ' || o.spo2_percent || '%',
                       'Weight ' || o.weight_kg || ' kg', 'Height ' || o.height_cm || ' cm'),
                       '')
              FROM paramedic_observations o WHERE o.encounter_id = v.encounter_id)
       END AS vitals,
       CASE WHEN v.source = 'interface 4' THEN
           (SELECT string_agg(concat_ws(', ', x.complaint, x.duration), '; '
                              ORDER BY x.line_no)
              FROM visit_complaint x WHERE x.message_id = v.rx_message)
       ELSE
           (SELECT string_agg(x.item, '; ' ORDER BY x.line_no)
              FROM consultation_findings x
             WHERE x.prescription_id = v.prescription_id AND x.section = 'chief_complaint')
       END AS complaints,
       CASE WHEN v.source = 'interface 4' THEN
           (SELECT string_agg(COALESCE(x.display, x.code), '; ' ORDER BY x.line_no)
              FROM visit_diagnosis x WHERE x.message_id = v.rx_message)
       ELSE
           (SELECT string_agg(x.text, '; ' ORDER BY x.line_no)
              FROM diagnoses x WHERE x.prescription_id = v.prescription_id)
       END AS diagnoses,
       CASE WHEN v.source = 'interface 4' THEN
           (SELECT string_agg(concat_ws(' ', x.form, COALESCE(x.medication_name, x.generic_name),
                                        x.strength, x.dose, x.frequency, x.timing, x.duration),
                              '; ' ORDER BY x.line_no)
              FROM visit_drug_order x WHERE x.message_id = v.rx_message)
       ELSE
           (SELECT string_agg(concat_ws(' ', x.drug, x.dose, x.frequency, x.duration),
                              '; ' ORDER BY x.line_no)
              FROM prescription_items x WHERE x.prescription_id = v.prescription_id)
       END AS drugs,
       CASE WHEN v.source = 'interface 4' THEN
           NULLIF(concat_ws('; ',
               (SELECT string_agg(COALESCE(x.test_name, x.test_code), '; ' ORDER BY x.line_no)
                  FROM visit_lab_order x WHERE x.message_id = v.rx_message),
               (SELECT string_agg(COALESCE(x.procedure_name, x.procedure_code), '; '
                                  ORDER BY x.line_no)
                  FROM visit_radiology_order x WHERE x.message_id = v.rx_message)), '')
       ELSE
           (SELECT string_agg(x.name, '; ' ORDER BY x.line_no)
              FROM investigations x WHERE x.prescription_id = v.prescription_id)
       END AS tests,
       CASE WHEN v.source = 'interface 4' THEN
           (SELECT string_agg(x.instruction, '; ' ORDER BY x.line_no)
              FROM visit_advice x WHERE x.message_id = v.rx_message)
       ELSE
           (SELECT lp.advice FROM latest_prescriptions lp
             WHERE lp.prescription_id = v.prescription_id)
       END AS advice,
       COALESCE(v.follow_up::text, v.follow_up_text) AS follow_up,
       v.prescription_version,
       -- The recordings of this visit and their files: matched by key, or by
       -- the recording the visit is linked to.
       (SELECT array_agg(DISTINCT s) FROM (
            SELECT v.session_id AS s WHERE v.session_id IS NOT NULL
            UNION SELECT o.session_id FROM visit_objects o
             WHERE o.visit_key = v.visit_key OR o.session_id = v.session_id) z)
           AS session_ids,
       (SELECT array_agg(o.object_key ORDER BY o.session_id, o.part_no, o.version)
          FROM visit_objects o
         WHERE o.kind = 'audio' AND (o.visit_key = v.visit_key OR o.session_id = v.session_id))
           AS audio_objects,
       (SELECT array_agg(o.object_key ORDER BY o.session_id, o.part_no, o.version)
          FROM visit_objects o
         WHERE o.kind = 'json' AND (o.visit_key = v.visit_key OR o.session_id = v.session_id))
           AS json_objects,
       'visits/' || v.patient_id || '/' || v.visit_key || '/' AS archive_folder,
       v.source
  FROM visits v;

COMMENT ON VIEW v_visit_folder IS
    'One row per visit: demographics, vitals, the prescription as readable text, and the recordings with their cloud archive files. Filter by patient_id, or use patient_folder(patient_id).';

CREATE FUNCTION patient_folder(patient TEXT) RETURNS SETOF v_visit_folder
    LANGUAGE sql STABLE AS
$$ SELECT * FROM v_visit_folder WHERE patient_id = patient
   ORDER BY visit_date DESC NULLS LAST, start_time DESC NULLS LAST $$;

COMMENT ON FUNCTION patient_folder(TEXT) IS
    'Every visit of one patient, newest first: SELECT * FROM patient_folder(''P4347460'').';
COMMENT ON TABLE visit_objects IS
    'Every file the cloud archive holds for a visit (audio and clinical JSON, each version), by visit_key.';

-- ---------------------------------------------------------------- who may do what
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_clinical_writer') THEN
        -- Rows are added, never changed; removed only with an erased recording.
        GRANT SELECT, INSERT, DELETE ON visit_objects TO aims_clinical_writer;
        GRANT USAGE, SELECT ON SEQUENCE visit_objects_id_seq TO aims_clinical_writer;
        GRANT SELECT ON v_visit_folder TO aims_clinical_writer;
        GRANT EXECUTE ON FUNCTION patient_folder(TEXT), visit_start_stamp(TEXT)
            TO aims_clinical_writer;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_clinical_reader') THEN
        GRANT SELECT ON visit_objects, v_visit_folder TO aims_clinical_reader;
        GRANT EXECUTE ON FUNCTION patient_folder(TEXT), visit_start_stamp(TEXT)
            TO aims_clinical_reader;
    END IF;
END $$;

COMMIT;
