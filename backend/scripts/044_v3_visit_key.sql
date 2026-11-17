-- ================================================================
-- 044 - each recording knows its visit's key, and each cloud copy its layout
--
-- The cloud archive kept copies by day: copies/<clinic>/<doctor>/<date>/.
-- Finding one patient meant knowing every day they came. From now on a copy
-- lives in its visit's folder:
--
--     visits/<patient_id>/<visit_key>/<file name>.vN.wav.enc | .json.enc
--
-- with visit_key = <patient_id>_<doctor_id>_<visit_id>, or, when CMED sent no
-- visit_id, <patient_id>_<doctor_id>_<YYYYMMDD>T<HHMMSS> from CMED's
-- start_time (api_v2.visit_key; clinical/006 computes the same).
--
--   sessions.visit_key            written when the recording is named
--   consultation_parts.visit_key  the same, for each patient's part
--   cloud_copies.layout           'by_day' (the old folders) or 'by_visit'
--   cloud_copies.copied_from      for a copy moved to its visit's folder by
--                                 scripts/backfill_visit_layout.py: the
--                                 object it was copied from. The old object
--                                 and its row stay.
--   v_visit_recordings            one row per visit: its recordings, whether
--                                 CMED confirmed them, and their copies
--
-- Additions only. Nothing is changed or removed. Safe to run twice.
-- Run as the tables' owner.
-- ================================================================
BEGIN;

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS visit_key TEXT;
ALTER TABLE consultation_parts ADD COLUMN IF NOT EXISTS visit_key TEXT;
ALTER TABLE cloud_copies ADD COLUMN IF NOT EXISTS layout VARCHAR(10) NOT NULL DEFAULT 'by_day';
ALTER TABLE cloud_copies ADD COLUMN IF NOT EXISTS copied_from TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'cloud_copies_layout_known') THEN
        ALTER TABLE cloud_copies ADD CONSTRAINT cloud_copies_layout_known
            CHECK (layout IN ('by_day', 'by_visit'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_sessions_visit_key ON sessions (visit_key)
    WHERE visit_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_parts_visit_key ON consultation_parts (visit_key)
    WHERE visit_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cloud_copies_copied_from ON cloud_copies (copied_from)
    WHERE copied_from IS NOT NULL;

DROP VIEW IF EXISTS v_visit_recordings;
CREATE VIEW v_visit_recordings AS
SELECT s.visit_key,
       s.patient_id, s.doctor_id, s.visit_id, s.hospital_id, s.session_date,
       array_agg(s.session_id ORDER BY s.opened_at)            AS session_ids,
       bool_or(s.confirmation = 'confirmed')                     AS confirmed,
       sum(s.total_duration_seconds)                             AS seconds,
       (SELECT array_agg(c.object_key ORDER BY c.session_id, c.version)
          FROM cloud_copies c
         WHERE c.session_id = ANY (array_agg(s.session_id)) AND c.layout = 'by_visit'
           AND c.kind = 'audio')                                 AS audio_objects,
       (SELECT array_agg(c.object_key ORDER BY c.session_id, c.version)
          FROM cloud_copies c
         WHERE c.session_id = ANY (array_agg(s.session_id)) AND c.layout = 'by_visit'
           AND c.kind = 'json')                                  AS json_objects
  FROM sessions s
 WHERE s.visit_key IS NOT NULL
 GROUP BY s.visit_key, s.patient_id, s.doctor_id, s.visit_id, s.hospital_id, s.session_date;

COMMENT ON VIEW v_visit_recordings IS
    'One row per visit: its recordings, whether CMED confirmed them, and their copies in the visit''s folder of the cloud archive.';
COMMENT ON COLUMN sessions.visit_key IS
    '<patient_id>_<doctor_id>_<visit_id> (or _<YYYYMMDD>T<HHMMSS> without a visit_id): the visit''s folder in the cloud archive.';
COMMENT ON COLUMN cloud_copies.layout IS
    'by_day: copies/<clinic>/<doctor>/<date>/; by_visit: visits/<patient_id>/<visit_key>/.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_api') THEN
        GRANT SELECT ON v_visit_recordings TO aims_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON v_visit_recordings TO aims_monitor;
    END IF;
END $$;

COMMIT;
