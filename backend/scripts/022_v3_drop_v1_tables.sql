-- ================================================================
-- 022 - drop the version 1 transcription tables nothing uses
--
--   doctor_reviews, health_screenings, ner_results, patients,
--   prescription_data, previous_visits, transcripts   (version 1's
--   transcription pipeline: never written by version 3, and the v1
--   worker that used them is not deployed)
--   used_grants   (its writer was never called; a grant is spent
--                 through grant_authorisations)
--
-- `patients` here is this database's. The clinical database's patients,
-- which CMED's data fills, is a different table and stays.
--
-- Refuses to run if any of them holds a row: dropping data is never a
-- side effect of tidying up. Children before parents, and no CASCADE, so
-- anything unexpected that still depends on one of them stops it too.
--
-- Safe to run twice. Run as the tables' owner.
-- ================================================================

BEGIN;

DO $$
DECLARE
    t text;
    n bigint;
BEGIN
    FOREACH t IN ARRAY ARRAY['doctor_reviews', 'health_screenings', 'ner_results',
                             'patients', 'prescription_data', 'previous_visits',
                             'transcripts', 'used_grants'] LOOP
        IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format('SELECT count(*) FROM %I', t) INTO n;
            IF n > 0 THEN
                RAISE EXCEPTION '022: % holds % row(s); nothing was dropped', t, n;
            END IF;
        END IF;
    END LOOP;
END $$;

DROP TABLE IF EXISTS health_screenings;
DROP TABLE IF EXISTS previous_visits;
DROP TABLE IF EXISTS patients;
DROP TABLE IF EXISTS doctor_reviews;
DROP TABLE IF EXISTS prescription_data;
DROP TABLE IF EXISTS ner_results;
DROP TABLE IF EXISTS transcripts;
DROP TABLE IF EXISTS used_grants;

COMMIT;
