-- ================================================================
-- Removes the TEST data from the REAL server (BETA_SERVER.md, step 9), so the
-- real server holds the field only. Every row removed here is already on the
-- beta server (a full copy made 24 Nov 2026 01:10 Dhaka).
--
--   dry run (counts, then ROLLBACK):
--     docker exec -i aims-postgres psql -U aims_admin -v apply=0 -f - < beta/remove_test_from_real.sql
--   apply (COMMIT) - only with the owner's written yes:
--     docker exec -i aims-postgres psql -U aims_admin -v apply=1 -f - < beta/remove_test_from_real.sql
--
-- The field, as the owner set it on 24 Nov 2026 - everything else is testing:
--   AALO_KARAIL      (CMED 10034140)        from 23 Nov 2026 16:29 Dhaka - GP-Males, GP-Female
--   AMADER_SUSASTHO  (CMED Amader-Susastho) from 22 Nov 2026 15:00 Dhaka - Lenovo
--
-- Database rows only. No object in any bucket is touched: beta uses the same
-- copy-store objects. Kept as they are: the audit logs (append-only), the PC
-- and clinic rows (the audit log refers to them; the test PCs are revoked),
-- people, places and CMED's logins.
-- ================================================================
\set ON_ERROR_STOP on
\connect aims_recordings
-- Never on beta: beta's database carries aims_beta_marker.
SELECT to_regclass('public.aims_beta_marker') IS NULL AS is_real \gset
\if :is_real
\echo '== this is the real database: going on'
\else
\echo '== this is the BETA database: nothing done'
\quit
\endif
BEGIN;

CREATE TEMP TABLE cut (hospital_id text, cmed text, pc text, since timestamptz, client text);
INSERT INTO cut VALUES
  ('AALO_KARAIL', '10034140', 'GP-Males', '2026-11-23 16:29:00+06', 'aalo_clinic_karail'),
  ('AALO_KARAIL', '10034140', 'GP-Female', '2026-11-23 16:29:00+06', 'aalo_clinic_karail'),
  ('AMADER_SUSASTHO', 'Amader-Susastho', 'Lenovo', '2026-11-22 15:00:00+06', 'amader-susastho-1');

CREATE TEMP TABLE real_devices AS
  SELECT DISTINCT d.device_id, c.since FROM devices d JOIN cut c ON c.pc = d.machine_name
   AND c.hospital_id = d.hospital_id;
CREATE TEMP TABLE real_sessions AS
  SELECT s.session_id FROM sessions s JOIN devices d ON d.device_id = s.device_id
    JOIN cut c ON c.pc = d.machine_name AND c.hospital_id = s.hospital_id
   WHERE s.opened_at >= c.since;
CREATE TEMP TABLE test_sessions AS
  SELECT session_id FROM sessions WHERE session_id NOT IN (SELECT session_id FROM real_sessions);
CREATE TEMP TABLE real_grants AS
  SELECT g.jti FROM grant_authorisations g JOIN real_devices r ON r.device_id = g.device_id
   WHERE g.issued_at >= r.since
  UNION SELECT s.grant_jti FROM sessions s JOIN real_sessions USING (session_id)
   WHERE s.grant_jti IS NOT NULL;

\echo '== aims_recordings: what goes, and what stays'
SELECT (SELECT count(*) FROM test_sessions) AS test_recordings_go,
       (SELECT count(*) FROM real_sessions) AS field_recordings_stay,
       (SELECT count(*) FROM grant_authorisations
         WHERE jti NOT IN (SELECT jti FROM real_grants)) AS test_grants_go;

DELETE FROM portal_batch_files WHERE session_id IN (SELECT session_id FROM test_sessions);
DELETE FROM portal_downloads WHERE session_id IN (SELECT session_id FROM test_sessions);
DELETE FROM collector_notifications n
 WHERE NOT (n.session_id IN (SELECT session_id FROM real_sessions)
            OR (n.session_id IS NULL AND EXISTS (
                SELECT 1 FROM cut c WHERE c.hospital_id = n.hospital_id AND n.created_at >= c.since)));
DELETE FROM integrity_alerts WHERE session_id IN (SELECT session_id FROM test_sessions)
   OR (session_id IS NULL AND device_id NOT IN (SELECT device_id FROM real_devices));
DELETE FROM erase_tasks WHERE session_id IN (SELECT session_id FROM test_sessions);
DELETE FROM purge_receipts WHERE session_id IN (SELECT session_id FROM test_sessions);
DELETE FROM session_clicks WHERE session_id IN (SELECT session_id FROM test_sessions);
DELETE FROM session_refusals r
 WHERE r.session_id IN (SELECT session_id FROM test_sessions)
    OR NOT EXISTS (SELECT 1 FROM real_devices d WHERE d.device_id = r.device_id AND r.refused_at >= d.since);
DELETE FROM consultation_parts WHERE session_id IN (SELECT session_id FROM test_sessions);
DELETE FROM cloud_copies WHERE session_id IN (SELECT session_id FROM test_sessions);
DELETE FROM segments WHERE session_id IN (SELECT session_id FROM test_sessions);
DELETE FROM chain_entries WHERE session_id IN (SELECT session_id FROM test_sessions);
DELETE FROM clips WHERE session_id IN (SELECT session_id FROM test_sessions);
DELETE FROM sessions WHERE session_id IN (SELECT session_id FROM test_sessions);
DELETE FROM confirmation_notices n
 WHERE (n.claimed_by_jti IS NULL OR n.claimed_by_jti NOT IN (SELECT jti FROM real_grants))
   AND NOT EXISTS (SELECT 1 FROM cut c WHERE c.hospital_id = n.hospital_id AND n.received_at >= c.since);
DELETE FROM grant_authorisations WHERE jti NOT IN (SELECT jti FROM real_grants);
DELETE FROM device_events e
 WHERE e.device_id IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM real_devices d WHERE d.device_id::text = e.device_id AND e.last_at >= d.since);
DELETE FROM device_health_log h
 WHERE NOT EXISTS (SELECT 1 FROM real_devices d WHERE d.device_id = h.device_id AND h.received_at >= d.since);
DELETE FROM cmed_request_log l
 WHERE NOT EXISTS (SELECT 1 FROM cut c WHERE c.client = l.client_id AND l.at >= c.since);

\echo '== aims_recordings: what is left'
SELECT s.hospital_id, d.machine_name, count(*) AS recordings,
       to_char(min(s.opened_at) AT TIME ZONE 'Asia/Dhaka', 'DD Mon HH24:MI') AS oldest,
       to_char(max(s.opened_at) AT TIME ZONE 'Asia/Dhaka', 'DD Mon HH24:MI') AS newest
  FROM sessions s JOIN devices d ON d.device_id = s.device_id GROUP BY 1, 2 ORDER BY 1, 2;

\if :apply
COMMIT;
\echo '== aims_recordings: COMMITTED'
\else
ROLLBACK;
\echo '== aims_recordings: dry run, ROLLED BACK'
\endif

-- ---------------------------------------------------------------- clinical
\connect aims_clinical
BEGIN;
CREATE TEMP TABLE cut (hospital_id text, cmed text, since timestamptz);
INSERT INTO cut VALUES
  ('AALO_KARAIL', '10034140', '2026-11-23 16:29:00+06'),
  ('AMADER_SUSASTHO', 'Amader-Susastho', '2026-11-22 15:00:00+06');

-- A field visit: CMED's first message for it came after its clinic's time.
CREATE TEMP TABLE real_visits AS
  SELECT v.id, v.visit_key FROM patient_visit v JOIN cut c ON c.cmed = v.hospital_id
   WHERE COALESCE(v.patient_information_at, v.created_at) >= c.since;
CREATE TEMP TABLE test_visits AS
  SELECT id, visit_key, hospital_id, hospital_patient_id FROM patient_visit
   WHERE id NOT IN (SELECT id FROM real_visits);

\echo '== aims_clinical: what goes, and what stays'
SELECT (SELECT count(*) FROM test_visits) AS test_visits_go,
       (SELECT count(*) FROM real_visits) AS field_visits_stay;

DELETE FROM visit_objects o
 WHERE o.visit_key IS NULL OR o.visit_key NOT IN (SELECT visit_key FROM real_visits WHERE visit_key IS NOT NULL);
DELETE FROM visit_advice WHERE visit_pk IN (SELECT id FROM test_visits);
DELETE FROM visit_complaint WHERE visit_pk IN (SELECT id FROM test_visits);
DELETE FROM visit_diagnosis WHERE visit_pk IN (SELECT id FROM test_visits);
DELETE FROM visit_drug_order WHERE visit_pk IN (SELECT id FROM test_visits);
DELETE FROM visit_lab_order WHERE visit_pk IN (SELECT id FROM test_visits);
DELETE FROM visit_observation WHERE visit_pk IN (SELECT id FROM test_visits);
DELETE FROM visit_radiology_order WHERE visit_pk IN (SELECT id FROM test_visits);
DELETE FROM visit_vital WHERE visit_pk IN (SELECT id FROM test_visits);
DELETE FROM patient_visit WHERE id IN (SELECT id FROM test_visits);
DELETE FROM visit_messages m
 WHERE NOT EXISTS (SELECT 1 FROM cut c WHERE c.cmed = m.hospital_id AND m.received_at >= c.since)
   AND NOT EXISTS (SELECT 1 FROM patient_visit v WHERE v.patient_information_message_id = m.id
                                                    OR v.prescription_message_id = m.id);
-- encounters (and their prescriptions and details, by cascade)
DELETE FROM encounters e
 WHERE NOT EXISTS (SELECT 1 FROM cut c WHERE c.hospital_id = e.hospital_id AND e.created_at >= c.since);
DELETE FROM intake_records i
 WHERE NOT EXISTS (SELECT 1 FROM cut c WHERE c.hospital_id = i.hospital_id AND i.received_at >= c.since)
   AND NOT EXISTS (SELECT 1 FROM encounters e WHERE e.patient_information_id = i.id)
   AND NOT EXISTS (SELECT 1 FROM prescriptions p WHERE p.intake_id = i.id);
-- patients left with no visit or encounter at all
DELETE FROM patient p
 WHERE NOT EXISTS (SELECT 1 FROM patient_visit v WHERE v.hospital_id = p.hospital_id
                                                  AND v.hospital_patient_id = p.hospital_patient_id);
DELETE FROM patients p
 WHERE NOT EXISTS (SELECT 1 FROM encounters e WHERE e.patient_id = p.patient_id)
   AND NOT EXISTS (SELECT 1 FROM female_details f WHERE f.patient_id = p.patient_id)
   AND NOT EXISTS (SELECT 1 FROM male_details m WHERE m.patient_id = p.patient_id);

\echo '== aims_clinical: what is left per clinic'
SELECT hospital_id, count(*) AS visits,
       to_char(min(COALESCE(patient_information_at, created_at)) AT TIME ZONE 'Asia/Dhaka', 'DD Mon HH24:MI') AS oldest,
       to_char(max(COALESCE(patient_information_at, created_at)) AT TIME ZONE 'Asia/Dhaka', 'DD Mon HH24:MI') AS newest
  FROM patient_visit GROUP BY 1 ORDER BY 1;

\if :apply
COMMIT;
\echo '== aims_clinical: COMMITTED'
\else
ROLLBACK;
\echo '== aims_clinical: dry run, ROLLED BACK'
\endif
