-- ================================================================
-- 024 - every PC, and every table, readable in the database viewer
--
--   device_nonces       each row now belongs to a PC the database knows
--                       (a foreign key, removed with the PC), and
--                       v_device_requests shows it with the PC's name and
--                       clinic instead of a bare id
--   v_devices           one row per recorder PC: clinic, name, state,
--                       version, where its key lives, what it holds, when
--                       it was last heard from
--   table comments      one line on what each table is for, which the
--                       viewer shows beside its name
--
-- Safe to run twice. Run as the tables' owner.
-- ================================================================

BEGIN;

-- A nonce is fifteen minutes of replay protection and nothing more, so one
-- left by a PC that no longer exists is dropped, not kept.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'device_nonces_device_fk') THEN
        DELETE FROM device_nonces n
         WHERE NOT EXISTS (SELECT 1 FROM devices d WHERE d.device_id = n.device_id);
        ALTER TABLE device_nonces
            ADD CONSTRAINT device_nonces_device_fk FOREIGN KEY (device_id)
            REFERENCES devices (device_id) ON DELETE CASCADE;
    END IF;
END $$;

-- v_devices counts each PC's open alerts.
CREATE INDEX IF NOT EXISTS idx_alerts_open_device
    ON integrity_alerts (device_id)
    WHERE resolved_at IS NULL AND device_id IS NOT NULL;

DROP VIEW IF EXISTS v_device_requests;
DROP VIEW IF EXISTS v_devices;

-- "silent" is the dashboard's rule: no heartbeat for three minutes (three
-- missed beats of one a minute).
CREATE VIEW v_devices AS
SELECT
    d.hospital_id,
    h.name                                            AS hospital,
    d.machine_name,
    CASE WHEN d.revoked_at IS NOT NULL THEN 'revoked'
         WHEN d.last_seen_at IS NULL THEN 'enrolled, never heard from'
         WHEN d.last_seen_at < now() - interval '3 minutes' THEN 'silent'
         ELSE 'online' END                            AS state,
    d.app_version,
    CASE octet_length(d.tpm_pubkey) WHEN 65 THEN 'TPM (P-256)'
                                    ELSE 'software (Ed25519)' END AS key_kind,
    d.signs_requests,
    d.disk_encrypted,
    d.last_seen_at,
    round(EXTRACT(EPOCH FROM now() - d.last_seen_at)::numeric / 60, 1)
                                                      AS minutes_since_seen,
    d.pending_segments,
    round(d.spool_bytes / 1048576.0, 1)               AS spool_mb,
    d.held_count,
    (SELECT count(*) FROM sessions s
      WHERE s.device_id = d.device_id
        AND s.session_date = (now() AT TIME ZONE COALESCE(h.timezone, 'Asia/Dhaka'))::date)
                                                      AS recordings_today,
    (SELECT max(s.opened_at) FROM sessions s WHERE s.device_id = d.device_id)
                                                      AS last_recording_at,
    (SELECT count(*) FROM integrity_alerts a
      WHERE a.device_id = d.device_id AND a.resolved_at IS NULL)
                                                      AS open_alerts,
    (SELECT count(*) FROM device_nonces n WHERE n.device_id = d.device_id)
                                                      AS signed_requests_15min,
    d.clock_offset_seconds,
    d.enrolled_at,
    d.revoked_at,
    d.revoked_reason,
    d.os_version,
    d.device_id
FROM devices d
LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
ORDER BY d.hospital_id, d.revoked_at IS NOT NULL, d.machine_name;

-- The signed requests of the last fifteen minutes, newest first, by PC.
CREATE VIEW v_device_requests AS
SELECT
    n.seen_at,
    d.hospital_id,
    h.name           AS hospital,
    d.machine_name,
    d.app_version,
    n.nonce,
    n.device_id
FROM device_nonces n
JOIN devices d        ON d.device_id = n.device_id
LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
ORDER BY n.seen_at DESC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON v_devices, v_device_requests TO aims_monitor;
    END IF;
END $$;

-- What each table is for. Tables a server may not have (clips comes from
-- version 1's own start-up) are skipped rather than failed on.
DO $$
DECLARE
    item text[];
BEGIN
    FOREACH item SLICE 1 IN ARRAY ARRAY[
        ['api_keys',             'Keys for service-to-service calls, stored as SHA-256; revoked_at ends one.'],
        ['audit_log',            'Append-only record of who did what: enrolments, revocations, erasures, admin actions.'],
        ['chain_entries',        'Each recording''s signed hash chain: one entry per clip, pause or stop, signed by the PC.'],
        ['clips',                'Version 1 clip records, still written for each uploaded clip; see segments for version 3.'],
        ['cloud_copies',         'Every encrypted copy of a WAV or its JSON in the cloud archive (R2), with its fingerprint.'],
        ['cmed_access_tokens',   'Access tokens issued to CMED''s servers, stored as SHA-256 (the binary key is the hash).'],
        ['cmed_assertion_jti',   'Signed assertions CMED has already used, kept so none can be replayed.'],
        ['cmed_client_keys',     'Public keys each CMED client signs its assertions with.'],
        ['cmed_clients',         'The CMED server clients allowed to call this API, with their allowed networks.'],
        ['confirmation_notices', 'CMED''s API 2 notices (Channel B): the five fields of a real visit, waiting to be matched.'],
        ['consultation_parts',   'A recording split into consultations when one recording held several patients.'],
        ['device_health_log',    'Each PC''s heartbeats and health reports, for the dashboard''s offline periods.'],
        ['device_nonces',        'Replay protection: the nonce of every signed request for 15 minutes, then swept. Read it through v_device_requests.'],
        ['devices',              'Every recorder PC: its clinic, public key, version and last contact. Read it through v_devices.'],
        ['doctors',              'Doctors by clinic, as the dashboard and the archive name them.'],
        ['enrollment_tokens',    'One-time enrolment tokens, stored as SHA-256; used_at and device_id say who redeemed one.'],
        ['erase_tasks',          'Files and cloud copies an erased recording left, waiting to be removed.'],
        ['fhir_messages',        'FHIR Bundle ids CMED has sent, so a reused id is recognised.'],
        ['grant_authorisations', 'Each grant to record a consultation, as the PC asked for it, and the session it opened.'],
        ['hospitals',            'The clinics, their CMED hospital id and their time zone.'],
        ['integrity_alerts',     'Everything that needs a person: mismatches, quarantines, silent PCs. resolved_at closes one.'],
        ['purge_receipts',       'Signed proof a clip is safely archived, which a PC needs before deleting its own copy.'],
        ['segments',             'Every uploaded clip of a recording: its object key, size, SHA-256 and state.'],
        ['session_clicks',       'The patient clicks CMED sent during a recording, used to split it into consultations.'],
        ['session_refusals',     'Consultations the patient refused, kept only as a digest so later data can be dropped.'],
        ['sessions',             'One row per recording. For a readable list use v_audio_files, v_audio_daily or v_audio_problems.'],
        ['v_audio_daily',        'Recordings per day and clinic, with how many are saved, in the cloud and need attention.'],
        ['v_audio_problems',     'Every recording that needs looking at, with the reasons, worst first. Filter by day and hospital_id.'],
        ['v_audio_files',        'One row per recording with its file name, place in the archive and fingerprint.'],
        ['v_devices',            'One row per recorder PC: clinic, name, online or silent, version, what it holds.'],
        ['v_device_requests',    'Signed requests of the last 15 minutes, newest first, with the PC and clinic.']
    ] LOOP
        IF to_regclass('public.' || item[1]) IS NULL THEN
            CONTINUE;
        END IF;
        -- Only the owner may comment; a description is not worth failing over.
        IF NOT pg_has_role(current_user, (SELECT relowner FROM pg_class
                                           WHERE oid = to_regclass('public.' || item[1])),
                           'USAGE') THEN
            RAISE NOTICE 'no comment on %: owned by another role', item[1];
        ELSE
            EXECUTE format('COMMENT ON %s public.%I IS %L',
                           CASE WHEN item[1] LIKE 'v\_%' THEN 'VIEW' ELSE 'TABLE' END,
                           item[1], item[2]);
        END IF;
    END LOOP;
END $$;

COMMIT;
