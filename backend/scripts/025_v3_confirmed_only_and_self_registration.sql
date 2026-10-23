-- ================================================================
-- 025 - keep only what CMED confirms; PCs that register themselves
--
-- 1. Only a recording CMED's server confirms is kept.
--    sessions.erase_after         when a recording CMED has not confirmed
--                                 is erased (the sweep sets it: opened_at
--                                 plus AIMS_UNCONFIRMED_ERASE_DAYS, 7 by
--                                 default); cleared the moment it is
--                                 confirmed, here in the database, so no
--                                 path that confirms can forget to
--    hospitals.cmed_last_message_at  the last API 2 or API 3 from CMED for
--                                 the clinic: a clinic CMED has gone quiet
--                                 for erases nothing (an outage is not a
--                                 reason to lose consultations)
--
-- 2. A clinic PC can register itself, without an enrolment token.
--    devices.approval             approved (every PC enrolled with a token),
--                                 pending (registered itself: uploads, but
--                                 capped), rejected
--    devices.registered_via       token | self
--    a trigger approves a pending PC once CMED's server has confirmed two of
--    its recordings - whatever path confirmed them
--
-- 3. The viewer: when each waiting recording will be erased, and each PC's
--    approval.
--
-- Safe to run twice. Run as the tables' owner.
-- ================================================================

BEGIN;

-- ---------------------------------------------------------------- 1
ALTER TABLE sessions  ADD COLUMN IF NOT EXISTS erase_after TIMESTAMP WITH TIME ZONE;
ALTER TABLE hospitals ADD COLUMN IF NOT EXISTS cmed_last_message_at TIMESTAMP WITH TIME ZONE;

CREATE INDEX IF NOT EXISTS idx_sessions_erase_after
    ON sessions (erase_after) WHERE erase_after IS NOT NULL;

-- ---------------------------------------------------------------- 2
ALTER TABLE devices ADD COLUMN IF NOT EXISTS approval VARCHAR(16) NOT NULL DEFAULT 'approved';
ALTER TABLE devices ADD COLUMN IF NOT EXISTS registered_via VARCHAR(8) NOT NULL DEFAULT 'token';
ALTER TABLE devices ADD COLUMN IF NOT EXISTS registered_ip TEXT;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS approved_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS approved_by TEXT;
-- The sweep writes the audit entry for an approval the trigger made (the
-- audit log is hash-chained by the server, so the trigger cannot).
ALTER TABLE devices ADD COLUMN IF NOT EXISTS approval_audited BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE devices DROP CONSTRAINT IF EXISTS devices_approval_check;
ALTER TABLE devices ADD CONSTRAINT devices_approval_check
    CHECK (approval IN ('pending', 'approved', 'rejected'));
ALTER TABLE devices DROP CONSTRAINT IF EXISTS devices_registered_via_check;
ALTER TABLE devices ADD CONSTRAINT devices_registered_via_check
    CHECK (registered_via IN ('token', 'self'));

CREATE INDEX IF NOT EXISTS idx_devices_pending
    ON devices (hospital_id, enrolled_at) WHERE approval = 'pending';
CREATE INDEX IF NOT EXISTS idx_devices_registered_ip
    ON devices (registered_ip, enrolled_at) WHERE registered_via = 'self';

-- Confirmed: the deadline goes, before the row is written.
CREATE OR REPLACE FUNCTION aims_clear_erase_deadline() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.confirmation = 'confirmed' THEN
        NEW.erase_after := NULL;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS sessions_clear_erase_deadline ON sessions;
CREATE TRIGGER sessions_clear_erase_deadline
    BEFORE INSERT OR UPDATE OF confirmation ON sessions
    FOR EACH ROW EXECUTE FUNCTION aims_clear_erase_deadline();

-- Two recordings confirmed by CMED's server approve the PC that made them.
-- A confirmation needs CMED to name the exact patient, doctor, clinic, date
-- and second of a visit this PC asked to record: only the PC in that room can
-- match it. Change the 2 here, and in api_v2.SELF_APPROVAL_CONFIRMATIONS.
CREATE OR REPLACE FUNCTION aims_approve_confirmed_device() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.confirmation = 'confirmed' AND NEW.device_id IS NOT NULL
       AND (TG_OP = 'INSERT' OR OLD.confirmation IS DISTINCT FROM 'confirmed') THEN
        UPDATE devices d
           SET approval = 'approved', approved_at = now(), approved_by = 'cmed',
               approval_audited = false
         WHERE d.device_id = NEW.device_id
           AND d.approval = 'pending'
           AND d.revoked_at IS NULL
           AND (SELECT count(*) FROM sessions s
                 WHERE s.device_id = NEW.device_id
                   AND s.confirmation = 'confirmed') >= 2;
    END IF;
    RETURN NULL;
END $$;

DROP TRIGGER IF EXISTS sessions_approve_confirmed_device ON sessions;
CREATE TRIGGER sessions_approve_confirmed_device
    AFTER INSERT OR UPDATE OF confirmation ON sessions
    FOR EACH ROW EXECUTE FUNCTION aims_approve_confirmed_device();

-- ---------------------------------------------------------------- 3
DROP VIEW IF EXISTS v_audio_daily;
DROP VIEW IF EXISTS v_audio_problems;
DROP VIEW IF EXISTS v_device_requests;
DROP VIEW IF EXISTS v_devices;

-- As 023, with two changes: only a confirmed recording is copied to the
-- cloud, so only a confirmed one can be late; and a waiting one says when it
-- will be erased.
CREATE VIEW v_audio_problems AS
SELECT
    s.session_date                          AS day,
    s.hospital_id,
    h.name                                  AS hospital,
    CASE x.worst WHEN 1 THEN 'critical' WHEN 2 THEN 'warning' ELSE 'info' END
                                            AS severity,
    x.problems,
    s.erase_after,
    s.doctor_id,
    s.patient_id,
    s.local_start                           AS started,
    s.local_end                             AS ended,
    s.file_stem || '.wav'                   AS file_name,
    s.status,
    s.confirmation,
    round(s.total_duration_seconds, 1)      AS duration_seconds,
    s.segment_count                         AS clips,
    s.archive_relpath                       AS file_location,
    s.session_id                            AS internal_key
FROM sessions s
LEFT JOIN hospitals h ON h.hospital_id = s.hospital_id
CROSS JOIN LATERAL (
    SELECT string_agg(p.problem, '; ' ORDER BY p.rank, p.problem) AS problems,
           min(p.rank) AS worst
      FROM (
        SELECT DISTINCT i.rank, i.problem FROM (
            SELECT v.rank, v.problem
              FROM (VALUES
                (1, CASE WHEN s.status = 'quarantined' OR s.quarantine_reason IS NOT NULL
                         THEN 'corrupted, held for review: '
                              || COALESCE(s.quarantine_reason, 'no reason given') END),
                (1, CASE WHEN s.archived_at IS NOT NULL
                          AND (s.archive_relpath IS NULL OR s.archive_sha256 IS NULL)
                         THEN 'marked saved but no WAV is recorded' END),
                (1, CASE WHEN s.archived_at IS NOT NULL AND s.chain_verified_at IS NULL
                         THEN 'saved without its hash chain verified' END),
                (1, CASE WHEN s.erase_after IS NOT NULL
                          AND s.confirmation IN ('pending', 'unconfirmed', 'legacy')
                          AND s.erase_after < now() + interval '48 hours'
                         THEN 'erased ' || to_char(s.erase_after AT TIME ZONE
                                                   COALESCE(h.timezone, 'Asia/Dhaka'),
                                                   'YYYY-MM-DD HH24:MI')
                              || ' unless CMED confirms it' END),
                (2, CASE WHEN s.closed_at < now() - interval '12 hours'
                          AND s.archived_at IS NULL
                          AND s.status NOT IN ('quarantined', 'expired', 'refused')
                          AND s.confirmation <> 'pending'
                         THEN 'ended over 12 h ago, WAV not built' END),
                (2, CASE WHEN s.closed_at IS NULL AND s.status = 'active'
                          AND s.opened_at < now() - interval '12 hours'
                         THEN 'still open after 12 h' END),
                (2, CASE WHEN s.archived_at < now() - interval '24 hours'
                          AND s.copied_at IS NULL AND s.confirmation = 'confirmed'
                         THEN 'not copied to the cloud after 24 h' END),
                (2, CASE WHEN s.archived_at IS NOT NULL AND s.total_duration_seconds < 5
                         THEN 'under 5 seconds of audio' END),
                (2, CASE WHEN s.confirmation = 'unconfirmed'
                         THEN 'never confirmed by CMED' END),
                -- The normal endings, as api_v2.NORMAL_CLOSE_REASONS lists them.
                (2, CASE WHEN COALESCE(s.close_reason, '') NOT IN (
                              '', 'doctor_stopped', 'superseded_by_new_patient',
                              'counselling_limit', 'no_activity_limit', 'pc_shutdown',
                              'software_update')
                         THEN 'ended abnormally: ' || s.close_reason END)
              ) AS v(rank, problem)
             WHERE v.problem IS NOT NULL
            UNION ALL
            SELECT 1, count(*) || ' clip(s) quarantined'
              FROM segments g
             WHERE g.session_id = s.session_id AND g.state = 'quarantined'
            HAVING count(*) > 0
            UNION ALL
            -- An unconfirmed recording's alert says what the line above says.
            SELECT CASE a.severity WHEN 'critical' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
                   'open alert: ' || replace(a.alert_type, '_', ' ')
              FROM integrity_alerts a
             WHERE a.session_id = s.session_id AND a.resolved_at IS NULL
               AND a.alert_type <> 'unconfirmed_recording'
        ) i
      ) p
) x
WHERE s.protocol_version >= 2
  AND s.confirmation NOT IN ('refused', 'expired')
  AND x.problems IS NOT NULL
ORDER BY s.session_date DESC, x.worst, s.hospital_id, s.local_start;

-- As 023, plus how many are waiting for CMED and how many were erased for
-- never being confirmed. "recordings" no longer counts the erased.
CREATE VIEW v_audio_daily AS
WITH first_day AS (
    SELECT min(session_date) AS day FROM sessions WHERE protocol_version >= 2
),
grid AS (
    SELECT d::date AS day, h.hospital_id
      FROM hospitals h
     CROSS JOIN first_day f
     CROSS JOIN generate_series(f.day, (now() AT TIME ZONE h.timezone)::date,
                                interval '1 day') AS d
     WHERE h.active
    UNION
    SELECT session_date, hospital_id
      FROM sessions
     WHERE protocol_version >= 2 AND session_date IS NOT NULL
),
counted AS (
    SELECT session_date AS day, hospital_id,
           count(*) FILTER (WHERE confirmation NOT IN ('refused', 'expired'))
                                                                       AS recordings,
           sum(total_duration_seconds) FILTER (WHERE confirmation NOT IN ('refused', 'expired'))
                                                                       AS seconds,
           count(*) FILTER (WHERE confirmation = 'confirmed')         AS confirmed,
           count(*) FILTER (WHERE confirmation IN ('pending', 'unconfirmed', 'legacy'))
                                                                       AS waiting_for_cmed,
           count(*) FILTER (WHERE archived_at IS NOT NULL
                              AND confirmation NOT IN ('refused', 'expired'))
                                                                       AS wav_saved,
           count(*) FILTER (WHERE copied_at IS NOT NULL)              AS in_cloud,
           count(*) FILTER (WHERE status IN ('active', 'closed'))     AS in_progress,
           count(*) FILTER (WHERE status = 'quarantined'
                              OR quarantine_reason IS NOT NULL)       AS corrupted,
           count(*) FILTER (WHERE confirmation = 'expired')           AS erased_unconfirmed,
           count(*) FILTER (WHERE confirmation = 'refused')           AS refused,
           sum(archive_bytes)                                         AS wav_bytes,
           min(local_start)                                           AS first_start,
           max(local_end)                                             AS last_end
      FROM sessions
     WHERE protocol_version >= 2
     GROUP BY session_date, hospital_id
),
flagged AS (
    SELECT day, hospital_id,
           count(*)                                       AS need_attention,
           count(*) FILTER (WHERE severity = 'critical')  AS critical
      FROM v_audio_problems
     GROUP BY day, hospital_id
)
SELECT
    g.day,
    g.hospital_id,
    h.name                                   AS hospital,
    COALESCE(c.recordings, 0)                AS recordings,
    round(COALESCE(c.seconds, 0) / 3600, 2)  AS hours,
    COALESCE(c.confirmed, 0)                 AS confirmed,
    COALESCE(c.waiting_for_cmed, 0)          AS waiting_for_cmed,
    COALESCE(c.wav_saved, 0)                 AS wav_saved,
    COALESCE(c.in_cloud, 0)                  AS in_cloud,
    COALESCE(c.in_progress, 0)               AS in_progress,
    COALESCE(c.corrupted, 0)                 AS corrupted,
    COALESCE(f.critical, 0)                  AS critical,
    COALESCE(f.need_attention, 0)            AS need_attention,
    COALESCE(c.erased_unconfirmed, 0)        AS erased_unconfirmed,
    COALESCE(c.refused, 0)                   AS refused,
    round(COALESCE(c.wav_bytes, 0) / 1e9, 2) AS wav_gb,
    c.first_start,
    c.last_end
FROM grid g
LEFT JOIN hospitals h ON h.hospital_id = g.hospital_id
LEFT JOIN counted c   ON c.day = g.day AND c.hospital_id = g.hospital_id
LEFT JOIN flagged f   ON f.day = g.day AND f.hospital_id = g.hospital_id
ORDER BY g.day DESC, g.hospital_id;

-- As 024, plus the PC's approval and how it joined.
CREATE VIEW v_devices AS
SELECT
    d.hospital_id,
    h.name                                            AS hospital,
    d.machine_name,
    CASE WHEN d.revoked_at IS NOT NULL THEN 'revoked'
         WHEN d.last_seen_at IS NULL THEN 'enrolled, never heard from'
         WHEN d.last_seen_at < now() - interval '3 minutes' THEN 'silent'
         ELSE 'online' END                            AS state,
    d.approval,
    d.registered_via,
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
    (SELECT count(*) FROM sessions s
      WHERE s.device_id = d.device_id AND s.confirmation = 'confirmed')
                                                      AS confirmed_recordings,
    (SELECT max(s.opened_at) FROM sessions s WHERE s.device_id = d.device_id)
                                                      AS last_recording_at,
    (SELECT count(*) FROM integrity_alerts a
      WHERE a.device_id = d.device_id AND a.resolved_at IS NULL)
                                                      AS open_alerts,
    (SELECT count(*) FROM device_nonces n WHERE n.device_id = d.device_id)
                                                      AS signed_requests_15min,
    d.clock_offset_seconds,
    d.enrolled_at,
    d.approved_at,
    d.approved_by,
    d.registered_ip,
    d.revoked_at,
    d.revoked_reason,
    d.os_version,
    d.device_id
FROM devices d
LEFT JOIN hospitals h ON h.hospital_id = d.hospital_id
ORDER BY d.hospital_id, d.revoked_at IS NOT NULL, d.machine_name;

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
        GRANT SELECT ON v_audio_problems, v_audio_daily, v_devices, v_device_requests
            TO aims_monitor;
    END IF;
END $$;

DO $$
DECLARE
    item text[];
BEGIN
    FOREACH item SLICE 1 IN ARRAY ARRAY[
        ['devices',           'Every recorder PC: clinic, public key, approval (pending until CMED confirms two of its recordings), last contact. Read it through v_devices.'],
        ['sessions',          'One row per recording. erase_after: when a recording CMED has not confirmed is erased. Readable: v_audio_files, v_audio_daily, v_audio_problems.'],
        ['v_audio_daily',     'Recordings per day and clinic: confirmed, waiting for CMED, saved, in the cloud, needing attention, erased as never confirmed.'],
        ['v_audio_problems',  'Every recording that needs looking at, worst first, with the date a waiting one will be erased. Filter by day and hospital_id.'],
        ['v_devices',         'One row per recorder PC: clinic, name, online or silent, approval, version, key, what it holds.'],
        ['v_device_requests', 'Signed requests of the last 15 minutes, newest first, with the PC and clinic.']
    ] LOOP
        IF to_regclass('public.' || item[1]) IS NULL THEN
            CONTINUE;
        END IF;
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
