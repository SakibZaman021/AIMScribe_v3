-- ================================================================
-- clinical/009 - the visit tables open in the database viewer
--
-- 19 Nov 2026: opening by_visit.demographics in the database viewer failed
-- with 'relation "visit_vital" does not exist'. Choosing the by_visit schema,
-- the viewer runs SET search_path TO by_visit - public is then not searched,
-- and two functions named their tables without it:
--
--   visit_vital_value()   used by by_visit.demographics for the vitals
--   patient_folder()      SELECT * FROM v_visit_folder
--
-- Both are replaced with every table named in full (public.visit_vital,
-- public.v_visit_folder), so they work whatever the search path. Same
-- arguments, same results; nothing stored changes. Safe to repeat.
-- Run as the owner (SET ROLE aims_clinical_owner).
-- ================================================================

BEGIN;

CREATE OR REPLACE FUNCTION public.visit_vital_value(message BIGINT, names TEXT[]) RETURNS TEXT
    LANGUAGE sql STABLE AS
$$ SELECT NULLIF(concat_ws(' ', x.value, x.unit), '') FROM public.visit_vital x
    WHERE x.message_id = message AND lower(btrim(x.name)) = ANY (names)
    ORDER BY x.group_no, x.item_no LIMIT 1 $$;

CREATE OR REPLACE FUNCTION public.patient_folder(patient TEXT) RETURNS SETOF public.v_visit_folder
    LANGUAGE sql STABLE AS
$$ SELECT * FROM public.v_visit_folder WHERE patient_id = patient
   ORDER BY visit_date DESC NULLS LAST, start_time DESC NULLS LAST $$;

COMMIT;
