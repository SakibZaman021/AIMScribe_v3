-- ================================================================
-- clinical/008 - one row per patient (by_visit.patient_list)
--
-- 18 Nov 2026: the user still had to scroll row after row to see who a
-- patient is. by_visit (007) holds one row per visit; this adds one row per
-- patient, newest first, with what a person needs to know them by:
--
--     clinic, cmed_clinic_code, patient_id, sex, date_of_birth, age (at the
--     latest visit), blood_group, visits, first_visit, last_visit,
--     last_visit_id, last_doctor_id, last_diagnoses, recordings, archive_folders
--
--     SELECT * FROM by_visit.patient_list WHERE patient_id = 'P4347460';
--
-- A patient is a CMED patient_id at a CMED clinic code. Built on
-- public.visit_index and by_visit.demographics (007), so it reads both copies
-- of the data, each visit once. Plain text: a CSV export opens as it reads.
--
-- Additions only: one view. Run as the owner (SET ROLE aims_clinical_owner).
-- Safe to repeat.
-- ================================================================

BEGIN;

DROP VIEW IF EXISTS by_visit.patient_list;

CREATE VIEW by_visit.patient_list AS
WITH visits AS (
    SELECT v.*, row_number() OVER (PARTITION BY v.cmed_clinic_code, v.patient_id
                                   ORDER BY v.visit_date DESC NULLS LAST,
                                            v.starts_at DESC NULLS LAST) AS newest
      FROM public.visit_index v
), seen AS (
    SELECT d.cmed_clinic_code, d.patient_id, d.sex, d.date_of_birth, d.age, d.blood_group,
           row_number() OVER (PARTITION BY d.cmed_clinic_code, d.patient_id
                              ORDER BY d.visit_date DESC NULLS LAST,
                                       d.start_time DESC NULLS LAST) AS newest
      FROM by_visit.demographics d
)
SELECT last.clinic,
       last.cmed_clinic_code,
       last.patient_id,
       s.sex,
       s.date_of_birth,
       s.age,
       (SELECT d.blood_group FROM by_visit.demographics d
         WHERE d.cmed_clinic_code = last.cmed_clinic_code AND d.patient_id = last.patient_id
           AND d.blood_group IS NOT NULL LIMIT 1)                       AS blood_group,
       (SELECT count(*) FROM visits a
         WHERE a.cmed_clinic_code = last.cmed_clinic_code
           AND a.patient_id = last.patient_id)                          AS visits,
       (SELECT min(a.visit_date) FROM visits a
         WHERE a.cmed_clinic_code = last.cmed_clinic_code
           AND a.patient_id = last.patient_id)                          AS first_visit,
       last.visit_date                                                  AS last_visit,
       last.visit_id                                                    AS last_visit_id,
       last.doctor_id                                                   AS last_doctor_id,
       (SELECT p.diagnoses FROM by_visit.prescription p
         WHERE p.visit_key = last.visit_key LIMIT 1)                    AS last_diagnoses,
       (SELECT count(DISTINCT o.session_id) FROM visit_objects o
         WHERE o.patient_id = last.patient_id)                          AS recordings,
       (SELECT string_agg(DISTINCT regexp_replace(o.object_key, '/[^/]*$', '/'), '; ')
          FROM visit_objects o WHERE o.patient_id = last.patient_id)    AS archive_folders
  FROM visits last
  LEFT JOIN seen s ON s.cmed_clinic_code = last.cmed_clinic_code
                  AND s.patient_id = last.patient_id AND s.newest = 1
 WHERE last.newest = 1
 ORDER BY last.visit_date DESC NULLS LAST, last.patient_id;

COMMENT ON VIEW by_visit.patient_list IS
    'One row per patient: sex, date of birth, age, blood group, how many visits, the latest visit and its diagnoses, the recordings and their archive folders. Newest first.';

DO $$
DECLARE
    who TEXT;
BEGIN
    FOREACH who IN ARRAY ARRAY['aims_clinical_writer', 'aims_clinical_reader', 'aims_researchers']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = who) THEN
            EXECUTE format('GRANT SELECT ON by_visit.patient_list TO %I', who);
        END IF;
    END LOOP;
END $$;

COMMIT;
