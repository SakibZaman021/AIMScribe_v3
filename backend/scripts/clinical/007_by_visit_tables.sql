-- ================================================================
-- clinical/007 - the visit tables a person opens first (schema by_visit)
--
-- The clinical database holds each CMED message twice (the interface-4
-- tables and the older encounter tables), across 28 tables keyed by numbers
-- no person can read (encounter_id, prescription_id, intake_id). Finding
-- whether one patient's API 2 arrived meant scrolling encounter_demographics.
--
-- Schema by_visit holds five views, and nothing else. Every row starts with
-- the same columns, the ones a person knows the visit by:
--
--     clinic, cmed_clinic_code, patient_id, doctor_id, visit_id,
--     visit_date, start_time (clinic time)
--
-- and every view lists the newest visit first:
--
--     by_visit.overview       one row per visit: did API 2 and API 3 arrive,
--                             the recording, its folder in the cloud archive
--     by_visit.demographics   one row per visit: sex, date of birth, age,
--                             blood group, blood pressure, pulse, temperature,
--                             weight, height, SpO2
--     by_visit.prescription   one row per visit: complaints, examination,
--                             diagnoses, medicines, investigations, advice,
--                             follow-up
--     by_visit.medicines      one row per medicine
--     by_visit.files          one row per file in the cloud archive
--
-- Plain text only - no JSON - so a CSV export opens as it reads. Both copies
-- of the data are read, each visit once (interface 4 when it has the visit,
-- else the older tables). Clinic time is Bangladesh time (+06:00 all year).
--
-- It also points v_visit_folder's archive_folder at where the files now are
-- (<hospital>/<doctor>/<date>/<patient>_<doctor>_<visit_id>/, 18 Nov 2026).
--
-- Additions only: a schema, views and functions; nothing stored changes.
-- Run as the owner (SET ROLE aims_clinical_owner). Safe to repeat.
-- ================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS by_visit;

-- A moment as clinic time, without needing a time-zone database.
CREATE OR REPLACE FUNCTION clinic_time(moment TIMESTAMPTZ) RETURNS TEXT
    LANGUAGE sql STABLE AS
$$ SELECT to_char((moment AT TIME ZONE 'UTC') + interval '6 hours', 'YYYY-MM-DD HH24:MI:SS') $$;

-- ---------------------------------------------------------------- one row per visit
DROP VIEW IF EXISTS by_visit.overview, by_visit.demographics, by_visit.prescription,
                    by_visit.medicines, by_visit.files;
DROP VIEW IF EXISTS visit_index;

CREATE VIEW visit_index AS
SELECT COALESCE(
           (SELECT e.hospital_id FROM encounters e
             WHERE e.cmed_hospital_id = pv.hospital_id AND e.patient_id = pv.hospital_patient_id
               AND e.source = 'live' AND e.start_time::timestamptz = pv.start_time
               AND e.hospital_id IS NOT NULL LIMIT 1),
           (SELECT o.hospital_id FROM visit_objects o
             WHERE o.visit_key = pv.visit_key AND o.hospital_id IS NOT NULL LIMIT 1))
                                                       AS clinic,
       pv.hospital_id                                  AS cmed_clinic_code,
       pv.hospital_patient_id                          AS patient_id,
       pv.practitioner_id                              AS doctor_id,
       pv.visit_id,
       pv.visit_date,
       to_char((pv.start_time AT TIME ZONE 'UTC') + interval '6 hours', 'HH24:MI:SS')
                                                       AS start_time,
       pv.start_time                                   AS starts_at,
       pv.visit_key,
       'interface 4'::text                             AS source,
       pv.id                                           AS visit_pk,
       pv.patient_information_message_id               AS info_message,
       pv.prescription_message_id                      AS rx_message,
       NULL::uuid                                      AS encounter_id,
       NULL::bigint                                    AS prescription_id,
       pv.patient_information_at                       AS api2_at,
       pv.prescription_at                              AS api3_at,
       pv.prescription_version,
       pv.issued_at,
       COALESCE(pv.follow_up::text, pv.follow_up_text) AS follow_up,
       pv.aims_id                                      AS session_id
  FROM patient_visit pv
UNION ALL
SELECT e.hospital_id, e.cmed_hospital_id, e.patient_id, e.doctor_id, NULL::varchar,
       e.visit_date, substring(e.start_time FROM 12 FOR 8), e.start_time::timestamptz,
       e.visit_key, 'interface 2'::text, NULL::bigint, NULL::bigint, NULL::bigint,
       e.encounter_id, lp.prescription_id,
       (SELECT d.created_at FROM encounter_demographics d WHERE d.encounter_id = e.encounter_id),
       (SELECT min(x.created_at) FROM prescriptions x WHERE x.encounter_id = e.encounter_id),
       COALESCE(lp.version, 0), lp.issued_at, lp.follow_up_date::text, e.session_id
  FROM encounters e
  LEFT JOIN latest_prescriptions lp ON lp.encounter_id = e.encounter_id
 WHERE e.source = 'live'
   AND NOT EXISTS (SELECT 1 FROM patient_visit pv
                    WHERE pv.hospital_id = e.cmed_hospital_id
                      AND pv.hospital_patient_id = e.patient_id
                      AND pv.start_time = e.start_time::timestamptz);

COMMENT ON VIEW visit_index IS
    'Every visit once, with the identifiers a person knows it by; what the by_visit views are built on.';

-- A vital sign from API 2, by any of the names CMED writes it under.
CREATE OR REPLACE FUNCTION visit_vital_value(message BIGINT, names TEXT[]) RETURNS TEXT
    LANGUAGE sql STABLE AS
$$ SELECT NULLIF(concat_ws(' ', x.value, x.unit), '') FROM visit_vital x
    WHERE x.message_id = message AND lower(btrim(x.name)) = ANY (names)
    ORDER BY x.group_no, x.item_no LIMIT 1 $$;

CREATE VIEW by_visit.overview AS
SELECT v.clinic, v.cmed_clinic_code, v.patient_id, v.doctor_id, v.visit_id, v.visit_date,
       v.start_time,
       v.api2_at IS NOT NULL                       AS api2_received,
       public.clinic_time(v.api2_at)               AS api2_received_at,
       v.api3_at IS NOT NULL                       AS api3_received,
       public.clinic_time(v.api3_at)               AS api3_received_at,
       v.prescription_version,
       (SELECT string_agg(DISTINCT s, ', ') FROM (
            SELECT v.session_id AS s WHERE v.session_id IS NOT NULL
            UNION SELECT o.session_id FROM visit_objects o WHERE o.visit_key = v.visit_key) z)
                                                   AS recordings,
       (SELECT count(*) FROM visit_objects o
         WHERE o.visit_key = v.visit_key AND o.kind = 'audio')  AS audio_files,
       (SELECT regexp_replace(min(o.object_key), '/[^/]*$', '/') FROM visit_objects o
         WHERE o.visit_key = v.visit_key)          AS archive_folder,
       v.visit_key, v.source
  FROM public.visit_index v
 ORDER BY v.visit_date DESC NULLS LAST, v.starts_at DESC NULLS LAST;

CREATE VIEW by_visit.demographics AS
SELECT v.clinic, v.cmed_clinic_code, v.patient_id, v.doctor_id, v.visit_id, v.visit_date,
       v.start_time,
       COALESCE(p.sex, d.sex)                      AS sex,
       COALESCE(p.date_of_birth, d.date_of_birth)  AS date_of_birth,
       COALESCE(pv.age, d.age_years::text)         AS age,
       COALESCE(pv.age_years, d.age_years::integer) AS age_years,
       p.blood_group,
       COALESCE(public.visit_vital_value(v.info_message, ARRAY['blood pressure', 'bp']),
                o.blood_pressure)                  AS blood_pressure,
       COALESCE(public.visit_vital_value(v.info_message, ARRAY['pulse', 'pulse rate', 'heart rate']),
                o.pulse_bpm::text)                 AS pulse,
       COALESCE(public.visit_vital_value(v.info_message, ARRAY['temperature', 'temp']),
                o.temperature_c::text)             AS temperature,
       COALESCE(public.visit_vital_value(v.info_message, ARRAY['weight']),
                o.weight_kg::text)                 AS weight,
       COALESCE(public.visit_vital_value(v.info_message, ARRAY['height']),
                o.height_cm::text)                 AS height,
       COALESCE(public.visit_vital_value(v.info_message,
                                         ARRAY['spo2', 'oxygen saturation', 'sp02']),
                o.spo2_percent::text)              AS spo2,
       public.clinic_time(v.api2_at)               AS received_at,
       v.visit_key
  FROM public.visit_index v
  LEFT JOIN patient_visit pv ON pv.id = v.visit_pk
  LEFT JOIN patient p ON p.hospital_id = pv.hospital_id
                     AND p.hospital_patient_id = pv.hospital_patient_id
  LEFT JOIN encounter_demographics d ON d.encounter_id = v.encounter_id
  LEFT JOIN paramedic_observations o ON o.encounter_id = v.encounter_id
 WHERE v.api2_at IS NOT NULL
 ORDER BY v.visit_date DESC NULLS LAST, v.starts_at DESC NULLS LAST;

CREATE VIEW by_visit.prescription AS
SELECT v.clinic, v.cmed_clinic_code, v.patient_id, v.doctor_id, v.visit_id, v.visit_date,
       v.start_time,
       CASE WHEN v.source = 'interface 4' THEN
           (SELECT string_agg(concat_ws(', ', x.complaint, x.duration, x.severity), '; '
                              ORDER BY x.line_no)
              FROM visit_complaint x WHERE x.message_id = v.rx_message)
       ELSE (SELECT string_agg(concat_ws(': ', x.item, x.value), '; ' ORDER BY x.line_no)
               FROM consultation_findings x
              WHERE x.prescription_id = v.prescription_id AND x.section = 'chief_complaint')
       END                                         AS chief_complaints,
       CASE WHEN v.source = 'interface 4' THEN
           (SELECT string_agg(concat_ws(': ', x.name, concat_ws(' ', x.value, x.unit)), '; '
                              ORDER BY x.group_no, x.item_no)
              FROM visit_observation x
             WHERE x.message_id = v.rx_message
               AND x.category IN ('on_examination', 'systemic_examination'))
       ELSE (SELECT string_agg(concat_ws(': ', x.item, x.value), '; ' ORDER BY x.line_no)
               FROM consultation_findings x
              WHERE x.prescription_id = v.prescription_id
                AND x.section IN ('on_examination', 'systemic_examination'))
       END                                         AS examination,
       CASE WHEN v.source = 'interface 4' THEN
           (SELECT string_agg(concat_ws(' ', COALESCE(x.display, x.code),
                                        '(' || NULLIF(x.code, '') || ')'), '; ' ORDER BY x.line_no)
              FROM visit_diagnosis x WHERE x.message_id = v.rx_message)
       ELSE (SELECT string_agg(x.text, '; ' ORDER BY x.line_no)
               FROM diagnoses x WHERE x.prescription_id = v.prescription_id)
       END                                         AS diagnoses,
       CASE WHEN v.source = 'interface 4' THEN
           (SELECT string_agg(concat_ws(' ', x.form, COALESCE(x.medication_name, x.generic_name),
                                        x.strength, x.dose, x.frequency, x.timing, x.duration),
                              '; ' ORDER BY x.line_no)
              FROM visit_drug_order x WHERE x.message_id = v.rx_message)
       ELSE (SELECT string_agg(concat_ws(' ', x.drug, x.dose, x.frequency, x.duration),
                               '; ' ORDER BY x.line_no)
               FROM prescription_items x WHERE x.prescription_id = v.prescription_id)
       END                                         AS medicines,
       CASE WHEN v.source = 'interface 4' THEN
           NULLIF(concat_ws('; ',
               (SELECT string_agg(COALESCE(x.test_name, x.test_code), '; ' ORDER BY x.line_no)
                  FROM visit_lab_order x WHERE x.message_id = v.rx_message),
               (SELECT string_agg(COALESCE(x.procedure_name, x.procedure_code), '; '
                                  ORDER BY x.line_no)
                  FROM visit_radiology_order x WHERE x.message_id = v.rx_message)), '')
       ELSE (SELECT string_agg(x.name, '; ' ORDER BY x.line_no)
               FROM investigations x WHERE x.prescription_id = v.prescription_id)
       END                                         AS investigations,
       CASE WHEN v.source = 'interface 4' THEN
           (SELECT string_agg(x.instruction, '; ' ORDER BY x.line_no)
              FROM visit_advice x WHERE x.message_id = v.rx_message)
       ELSE (SELECT x.advice FROM prescriptions x WHERE x.prescription_id = v.prescription_id)
       END                                         AS advice,
       v.follow_up,
       public.clinic_time(v.issued_at)             AS issued_at,
       v.prescription_version                      AS version,
       public.clinic_time(v.api3_at)               AS received_at,
       v.visit_key
  FROM public.visit_index v
 WHERE v.api3_at IS NOT NULL
 ORDER BY v.visit_date DESC NULLS LAST, v.starts_at DESC NULLS LAST;

CREATE VIEW by_visit.medicines AS
SELECT v.clinic, v.cmed_clinic_code, v.patient_id, v.doctor_id, v.visit_id, v.visit_date,
       v.start_time, m.line_no, m.form, m.medicine, m.generic_name, m.strength, m.dose,
       m.frequency, m.timing, m.duration, m.instruction, v.visit_key
  FROM public.visit_index v
  JOIN LATERAL (
      SELECT x.line_no, x.form, COALESCE(x.medication_name, x.generic_name) AS medicine,
             x.generic_name, x.strength, x.dose, x.frequency, x.timing, x.duration,
             x.instruction
        FROM visit_drug_order x
       WHERE v.source = 'interface 4' AND x.message_id = v.rx_message
      UNION ALL
      SELECT x.line_no, NULL, x.drug, NULL, NULL, x.dose, x.frequency, NULL, x.duration,
             x.instructions
        FROM prescription_items x
       WHERE v.source = 'interface 2' AND x.prescription_id = v.prescription_id
  ) m ON true
 ORDER BY v.visit_date DESC NULLS LAST, v.starts_at DESC NULLS LAST, m.line_no;

CREATE VIEW by_visit.files AS
SELECT COALESCE(o.hospital_id, v.clinic)           AS clinic,
       v.cmed_clinic_code, o.patient_id, o.doctor_id, COALESCE(o.visit_id, v.visit_id) AS visit_id,
       v.visit_date, v.start_time,
       CASE o.kind WHEN 'audio' THEN 'audio' ELSE 'CMED record (JSON)' END AS file,
       regexp_replace(o.object_key, '^.*/', '')    AS file_name,
       regexp_replace(o.object_key, '/[^/]*$', '/') AS folder,
       o.version, round(o.bytes / 1048576.0, 1)    AS size_mb,
       o.session_id AS recording, public.clinic_time(o.stored_at) AS stored_at,
       o.visit_key
  FROM visit_objects o
  LEFT JOIN public.visit_index v ON v.visit_key = o.visit_key
 ORDER BY v.visit_date DESC NULLS LAST, v.starts_at DESC NULLS LAST, o.object_key;

COMMENT ON SCHEMA by_visit IS
    'Start here: one row per visit (or per medicine, per file), newest first, identified by clinic, patient, doctor and visit.';
COMMENT ON VIEW by_visit.overview IS 'One row per visit: did API 2 and API 3 arrive, the recording, its archive folder.';
COMMENT ON VIEW by_visit.demographics IS 'One row per visit with API 2: sex, date of birth, age, blood group, vitals.';
COMMENT ON VIEW by_visit.prescription IS 'One row per visit with API 3: complaints, examination, diagnoses, medicines, investigations, advice, follow-up.';
COMMENT ON VIEW by_visit.medicines IS 'One row per medicine prescribed.';
COMMENT ON VIEW by_visit.files IS 'One row per file of a visit in the cloud archive.';

-- ---------------------------------------------------------------- v_visit_folder points at the new folders
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
       (SELECT regexp_replace(min(o.object_key), '/[^/]*$', '/') FROM visit_objects o
         WHERE o.visit_key = v.visit_key OR o.session_id = v.session_id)
           AS archive_folder,
       v.source
  FROM visits v;

COMMENT ON VIEW v_visit_folder IS
    'One row per visit: demographics, vitals, the prescription as readable text, and the recordings with their cloud archive files. Filter by patient_id, or use patient_folder(patient_id).';

CREATE FUNCTION patient_folder(patient TEXT) RETURNS SETOF v_visit_folder
    LANGUAGE sql STABLE AS
$$ SELECT * FROM v_visit_folder WHERE patient_id = patient
   ORDER BY visit_date DESC NULLS LAST, start_time DESC NULLS LAST $$;

-- ---------------------------------------------------------------- who may read them
DO $$
DECLARE
    who TEXT;
BEGIN
    FOREACH who IN ARRAY ARRAY['aims_clinical_writer', 'aims_clinical_reader', 'aims_researchers']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = who) THEN
            EXECUTE format('GRANT USAGE ON SCHEMA by_visit TO %I', who);
            EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA by_visit TO %I', who);
            EXECUTE format('GRANT SELECT ON public.visit_index TO %I', who);
            EXECUTE format('GRANT SELECT ON public.v_visit_folder TO %I', who);
            EXECUTE format('GRANT EXECUTE ON FUNCTION public.patient_folder(TEXT) TO %I', who);
            EXECUTE format('GRANT EXECUTE ON FUNCTION public.clinic_time(TIMESTAMPTZ), '
                           'public.visit_vital_value(BIGINT, TEXT[]) TO %I', who);
        END IF;
    END LOOP;
END $$;

COMMIT;
