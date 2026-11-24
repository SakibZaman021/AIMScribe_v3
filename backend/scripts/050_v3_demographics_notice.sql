-- ================================================================
-- 050 - when CMED's API 2 reached an open recording (25 Nov 2026)
--
-- Recorder 3.9.8 offers the doctor "Patient did not consent" only until
-- CMED's API 2 (demographics) for the visit arrives. The server notes the
-- moment on the recording and says so in its next heartbeat reply, as it
-- already does for API 3 (prescription_received_at).
--
-- Also: the audio-problems view counts an API 4 end as normal.
-- Adds only; nothing is deleted. Safe to run twice.
-- ================================================================
BEGIN;

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS demographics_received_at TIMESTAMP WITH TIME ZONE;

COMMENT ON COLUMN sessions.demographics_received_at IS
    'Recorder 3.9.8+: when CMED''s API 2 for this recording arrived while it was open.';

-- An end by CMED's API 4 ("consultation_ended") is a normal end, as
-- api_v2.NORMAL_CLOSE_REASONS lists it. The audio-problems view (025) names
-- the normal ends itself; this is the same view with that one end added.
CREATE OR REPLACE VIEW v_audio_problems AS
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
                              'software_update', 'consultation_ended')
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


COMMIT;
