-- ============================================================
-- 038: each researcher looks after named hospitals, and hears about them
--
-- Researchers still see and download every hospital. An admin also assigns
-- each researcher the hospitals they look after; for those, the researcher
-- gets a copy of every alert the data collectors get, word of whether the
-- collector dealt with it, and a report every evening - so the lab knows the
-- collection is going well, and that the collectors are on it.
--
-- Additions only: one new table and four new columns. An assignment that
-- ends is marked, never deleted.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS researcher_hospitals (
    email        VARCHAR(254) NOT NULL,
    hospital_id  VARCHAR(64) NOT NULL,
    added_by     VARCHAR(254),
    added_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    removed_at   TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (email, hospital_id)
);
CREATE INDEX IF NOT EXISTS idx_researcher_hospitals_hospital
    ON researcher_hospitals (hospital_id) WHERE removed_at IS NULL;

-- 'everything': every alert, whether it was handled, and the evening report.
-- 'problems': only what went wrong and stayed wrong, and the evening report.
ALTER TABLE portal_people ADD COLUMN IF NOT EXISTS alert_mode VARCHAR(16)
    NOT NULL DEFAULT 'everything';

-- Who an alert was for: the collector who must act, a researcher who looks
-- after the hospital (a copy, no Seen button), or AIMS LAB.
ALTER TABLE collector_notifications ADD COLUMN IF NOT EXISTS audience VARCHAR(16)
    NOT NULL DEFAULT 'collector';
-- When the researcher's copy was followed by "handled by ...".
ALTER TABLE collector_notifications ADD COLUMN IF NOT EXISTS followed_up_at
    TIMESTAMP WITH TIME ZONE;
CREATE INDEX IF NOT EXISTS idx_collector_notes_hospital_day
    ON collector_notifications (hospital_id, created_at);

COMMENT ON TABLE researcher_hospitals IS 'The hospitals each researcher looks after: alerts and an evening report for them. Ended assignments are marked, never deleted.';
COMMENT ON COLUMN portal_people.alert_mode IS 'everything or problems: how many alerts a researcher wants for their hospitals.';
COMMENT ON COLUMN collector_notifications.audience IS 'collector (must act), watcher (a researcher''s copy) or lab.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_api') THEN
        GRANT SELECT, INSERT, UPDATE ON researcher_hospitals TO aims_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON researcher_hospitals TO aims_monitor;
    END IF;
END $$;

COMMIT;
