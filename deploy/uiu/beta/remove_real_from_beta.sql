-- ================================================================
-- Removes the REAL field data from the BETA copy (BETA_SERVER.md, step 9),
-- so beta holds testing only. Run against the beta database server only:
--
--   dry run (counts, then ROLLBACK):
--     docker exec -i aimsbeta-postgres psql -U aims_admin -v apply=0 -f - < beta/remove_real_from_beta.sql
--   apply (COMMIT) - only with the owner's written yes:
--     docker exec -i aimsbeta-postgres psql -U aims_admin -v apply=1 -f - < beta/remove_real_from_beta.sql
--
-- Real field data, as the owner set it on 24 Nov 2026:
--   AALO_KARAIL      (CMED 10034140)        from 23 Nov 2026 16:29 Dhaka - GP-Males, GP-Female
--   AMADER_SUSASTHO  (CMED Amader-Susastho) from 22 Nov 2026 15:00 Dhaka - Lenovo
-- Everything before those times, and every other clinic and PC, is testing
-- and stays here.
--
-- Database rows only. No object in any bucket is touched: the real server
-- uses the same copy-store objects. The audit logs are append-only and keep
-- their lines. The PCs themselves stay (they also made test recordings).
-- ================================================================
\set ON_ERROR_STOP on
\connect aims_recordings
-- Never on the real server: only a database marked as beta (step 2 makes
-- the mark) is accepted; anywhere else this stops here.
SELECT to_regclass('public.aims_beta_marker') IS NOT NULL AS is_beta \gset
\if :is_beta
\echo '== this is the beta database: going on'
\else
\echo '== NOT the beta database (no aims_beta_marker): nothing done'
\quit
\endif
BEGIN;

CREATE TEMP TABLE cut (hospital_id text, cmed text, pc text, since timestamptz);
INSERT INTO cut VALUES
  ('AALO_KARAIL', '10034140', 'GP-Males', '2026-11-23 16:29:00+06'),
  ('AALO_KARAIL', '10034140', 'GP-Female', '2026-11-23 16:29:00+06'),
  ('AMADER_SUSASTHO', 'Amader-Susastho', 'Lenovo', '2026-11-22 15:00:00+06');

CREATE TEMP TABLE real_devices AS
  SELECT DISTINCT d.device_id, c.since FROM devices d JOIN cut c ON c.pc = d.machine_name
   AND c.hospital_id = d.hospital_id;
CREATE TEMP TABLE real_sessions AS
  SELECT s.session_id FROM sessions s JOIN devices d ON d.device_id = s.device_id
    JOIN cut c ON c.pc = d.machine_name AND c.hospital_id = s.hospital_id
   WHERE s.opened_at >= c.since;
CREATE TEMP TABLE real_grants AS
  SELECT g.jti FROM grant_authorisations g JOIN real_devices r ON r.device_id = g.device_id
   WHERE g.issued_at >= r.since
  UNION SELECT s.grant_jti FROM sessions s JOIN real_sessions USING (session_id)
   WHERE s.grant_jti IS NOT NULL;

\echo '== aims_recordings: what goes'
SELECT (SELECT count(*) FROM real_sessions) AS recordings,
       (SELECT count(*) FROM real_grants) AS grants,
       (SELECT count(*) FROM real_devices) AS pcs_kept_but_trimmed;

DELETE FROM portal_batch_files WHERE session_id IN (SELECT session_id FROM real_sessions);
DELETE FROM portal_downloads WHERE session_id IN (SELECT session_id FROM real_sessions);
DELETE FROM collector_notifications WHERE session_id IN (SELECT session_id FROM real_sessions)
   OR (hospital_id IN (SELECT hospital_id FROM cut)
       AND created_at >= (SELECT min(since) FROM cut c WHERE c.hospital_id = collector_notifications.hospital_id));
DELETE FROM integrity_alerts WHERE session_id IN (SELECT session_id FROM real_sessions);
DELETE FROM erase_tasks WHERE session_id IN (SELECT session_id FROM real_sessions);
DELETE FROM purge_receipts WHERE session_id IN (SELECT session_id FROM real_sessions);
DELETE FROM session_clicks WHERE session_id IN (SELECT session_id FROM real_sessions);
DELETE FROM session_refusals WHERE session_id IN (SELECT session_id FROM real_sessions)
   OR (device_id IN (SELECT device_id FROM real_devices)
       AND refused_at >= (SELECT since FROM real_devices r WHERE r.device_id = session_refusals.device_id));
DELETE FROM consultation_parts WHERE session_id IN (SELECT session_id FROM real_sessions);
DELETE FROM cloud_copies WHERE session_id IN (SELECT session_id FROM real_sessions);
DELETE FROM segments WHERE session_id IN (SELECT session_id FROM real_sessions);
DELETE FROM chain_entries WHERE session_id IN (SELECT session_id FROM real_sessions);
DELETE FROM clips WHERE session_id IN (SELECT session_id FROM real_sessions);
DELETE FROM sessions WHERE session_id IN (SELECT session_id FROM real_sessions);
DELETE FROM confirmation_notices WHERE claimed_by_jti IN (SELECT jti FROM real_grants)
   OR (hospital_id IN (SELECT hospital_id FROM cut)
       AND received_at >= (SELECT min(since) FROM cut c WHERE c.hospital_id = confirmation_notices.hospital_id));
DELETE FROM grant_authorisations WHERE jti IN (SELECT jti FROM real_grants);
DELETE FROM device_events WHERE device_id IN (SELECT device_id::text FROM real_devices)
   AND last_at >= (SELECT since FROM real_devices r WHERE r.device_id::text = device_events.device_id);
DELETE FROM device_health_log WHERE device_id IN (SELECT device_id FROM real_devices)
   AND received_at >= (SELECT since FROM real_devices r WHERE r.device_id = device_health_log.device_id);
DELETE FROM cmed_request_log WHERE at >= '2026-11-23 16:29:00+06' AND client_id = 'aalo_clinic_karail';
DELETE FROM cmed_request_log WHERE at >= '2026-11-22 15:00:00+06' AND client_id = 'amader-susastho-1';

\echo '== aims_recordings: what is left'
SELECT s.hospital_id, d.machine_name, count(*) AS recordings,
       to_char(max(s.opened_at) AT TIME ZONE 'Asia/Dhaka', 'DD Mon HH24:MI') AS newest
  FROM sessions s JOIN devices d ON d.device_id = s.device_id
 WHERE d.machine_name IN ('GP-Males', 'GP-Female', 'Lenovo')
 GROUP BY 1, 2 ORDER BY 1, 2;

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

-- A real visit: CMED's first message for it came after its clinic's time.
CREATE TEMP TABLE real_visits AS
  SELECT v.id, v.visit_key, v.hospital_id, v.visit_id, v.hospital_patient_id
    FROM patient_visit v JOIN cut c ON c.cmed = v.hospital_id
   WHERE COALESCE(v.patient_information_at, v.created_at) >= c.since;

\echo '== aims_clinical: what goes'
SELECT count(*) AS real_visits FROM real_visits;

DELETE FROM visit_objects WHERE visit_key IN (SELECT visit_key FROM real_visits);
DELETE FROM visit_advice WHERE visit_pk IN (SELECT id FROM real_visits);
DELETE FROM visit_complaint WHERE visit_pk IN (SELECT id FROM real_visits);
DELETE FROM visit_diagnosis WHERE visit_pk IN (SELECT id FROM real_visits);
DELETE FROM visit_drug_order WHERE visit_pk IN (SELECT id FROM real_visits);
DELETE FROM visit_lab_order WHERE visit_pk IN (SELECT id FROM real_visits);
DELETE FROM visit_observation WHERE visit_pk IN (SELECT id FROM real_visits);
DELETE FROM visit_radiology_order WHERE visit_pk IN (SELECT id FROM real_visits);
DELETE FROM visit_vital WHERE visit_pk IN (SELECT id FROM real_visits);
DELETE FROM patient_visit WHERE id IN (SELECT id FROM real_visits);
DELETE FROM visit_messages m USING cut c
 WHERE m.hospital_id = c.cmed AND m.received_at >= c.since
   AND NOT EXISTS (SELECT 1 FROM patient_visit v WHERE v.patient_information_message_id = m.id
                                                    OR v.prescription_message_id = m.id);
-- encounters (and their prescriptions, by cascade) of those visits
DELETE FROM encounters e USING cut c
 WHERE e.hospital_id = c.hospital_id AND e.created_at >= c.since;
DELETE FROM intake_records i USING cut c
 WHERE i.hospital_id = c.hospital_id AND i.received_at >= c.since
   AND NOT EXISTS (SELECT 1 FROM encounters e WHERE e.patient_information_id = i.id)
   AND NOT EXISTS (SELECT 1 FROM prescriptions p WHERE p.intake_id = i.id);
-- patients left with no visit at all
DELETE FROM patient p USING cut c
 WHERE p.hospital_id = c.cmed AND p.first_seen_at >= c.since
   AND NOT EXISTS (SELECT 1 FROM patient_visit v WHERE v.hospital_id = p.hospital_id
                                                  AND v.hospital_patient_id = p.hospital_patient_id);

\echo '== aims_clinical: what is left per clinic'
SELECT hospital_id, count(*) AS visits,
       to_char(max(COALESCE(patient_information_at, created_at)) AT TIME ZONE 'Asia/Dhaka', 'DD Mon HH24:MI') AS newest
  FROM patient_visit GROUP BY 1 ORDER BY 1;

\if :apply
COMMIT;
\echo '== aims_clinical: COMMITTED'
\else
ROLLBACK;
\echo '== aims_clinical: dry run, ROLLED BACK'
\endif
