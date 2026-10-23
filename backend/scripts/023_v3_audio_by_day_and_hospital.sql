-- ================================================================
-- 023 - the recordings, day by day and clinic by clinic
--
-- At a thousand recordings a day across the clinics, the sessions table is
-- too long to read. Two views to open in the database viewer instead:
--
--   v_audio_daily     one row per day and clinic: how many were recorded,
--                     how many are saved as WAV and copied to the cloud, and
--                     how many need looking at. A clinic that recorded
--                     nothing that day is listed with zeros, not left out.
--
--   v_audio_problems  one row per recording that needs looking at, with
--                     every reason on the same row, worst first. Filter it
--                     by day and hospital_id to go from a count in
--                     v_audio_daily to the files behind it.
--
-- What counts as a problem is decided here, from the same columns the
-- server writes:
--
--   critical  quarantined (failed a hash, the WAV check or the chain), a
--             clip quarantined, marked saved with no WAV recorded, saved
--             without its chain verified, any open critical alert
--   warning   ended over 12 hours ago and still no WAV, still open after
--             12 hours, saved but not copied to the cloud after 24 hours,
--             under 5 seconds of audio, never confirmed by CMED, ended
--             abnormally, any open warning alert
--
-- A refused consultation was erased on purpose, and an expired one was
-- never confirmed in time: neither is a problem with the file.
--
-- Safe to run twice. Run as the tables' owner.
-- ================================================================

BEGIN;

-- The views filter by day and clinic, and look up a recording's open alerts.
CREATE INDEX IF NOT EXISTS idx_sessions_day_hospital
    ON sessions (session_date, hospital_id);
CREATE INDEX IF NOT EXISTS idx_alerts_open_session
    ON integrity_alerts (session_id)
    WHERE resolved_at IS NULL AND session_id IS NOT NULL;

DROP VIEW IF EXISTS v_audio_daily;
DROP VIEW IF EXISTS v_audio_problems;

CREATE VIEW v_audio_problems AS
SELECT
    s.session_date                          AS day,
    s.hospital_id,
    h.name                                  AS hospital,
    CASE x.worst WHEN 1 THEN 'critical' WHEN 2 THEN 'warning' ELSE 'info' END
                                            AS severity,
    x.problems,
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
                (2, CASE WHEN s.closed_at < now() - interval '12 hours'
                          AND s.archived_at IS NULL
                          AND s.status NOT IN ('quarantined', 'expired', 'refused')
                          AND s.confirmation <> 'pending'
                         THEN 'ended over 12 h ago, WAV not built' END),
                (2, CASE WHEN s.closed_at IS NULL AND s.status = 'active'
                          AND s.opened_at < now() - interval '12 hours'
                         THEN 'still open after 12 h' END),
                (2, CASE WHEN s.archived_at < now() - interval '24 hours'
                          AND s.copied_at IS NULL
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
  AND x.problems IS NOT NULL
ORDER BY s.session_date DESC, x.worst, s.hospital_id, s.local_start;

CREATE VIEW v_audio_daily AS
WITH first_day AS (
    SELECT min(session_date) AS day FROM sessions WHERE protocol_version >= 2
),
-- Every active clinic on every day since the first recording, so a clinic
-- that recorded nothing shows a zero; and any day a clinic did record.
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
           count(*) FILTER (WHERE confirmation <> 'refused')          AS recordings,
           sum(total_duration_seconds) FILTER (WHERE confirmation <> 'refused')
                                                                       AS seconds,
           count(*) FILTER (WHERE archived_at IS NOT NULL)            AS wav_saved,
           count(*) FILTER (WHERE copied_at IS NOT NULL)              AS in_cloud,
           count(*) FILTER (WHERE status IN ('active', 'closed'))     AS in_progress,
           count(*) FILTER (WHERE status = 'quarantined'
                              OR quarantine_reason IS NOT NULL)       AS corrupted,
           count(*) FILTER (WHERE confirmation = 'unconfirmed')       AS unconfirmed,
           count(*) FILTER (WHERE confirmation = 'expired')           AS expired,
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
    COALESCE(c.wav_saved, 0)                 AS wav_saved,
    COALESCE(c.in_cloud, 0)                  AS in_cloud,
    COALESCE(c.in_progress, 0)               AS in_progress,
    COALESCE(c.corrupted, 0)                 AS corrupted,
    COALESCE(f.critical, 0)                  AS critical,
    COALESCE(f.need_attention, 0)            AS need_attention,
    COALESCE(c.unconfirmed, 0)               AS unconfirmed,
    COALESCE(c.expired, 0)                   AS expired,
    COALESCE(c.refused, 0)                   AS refused,
    round(COALESCE(c.wav_bytes, 0) / 1e9, 2) AS wav_gb,
    c.first_start,
    c.last_end
FROM grid g
LEFT JOIN hospitals h ON h.hospital_id = g.hospital_id
LEFT JOIN counted c   ON c.day = g.day AND c.hospital_id = g.hospital_id
LEFT JOIN flagged f   ON f.day = g.day AND f.hospital_id = g.hospital_id
ORDER BY g.day DESC, g.hospital_id;

-- The read-only monitor login browses them too.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON v_audio_problems, v_audio_daily TO aims_monitor;
    END IF;
END $$;

COMMIT;
