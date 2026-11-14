-- ============================================================
-- 036: data collectors - a third portal role, places, shifts, mails
--
-- A data collector signs in like a researcher but sees only the hospitals
-- of their place, never downloads, and is mailed what happens at those
-- hospitals while they are on shift. Every shift they start and end is kept
-- as an attendance row, which an admin downloads as a CSV for salaries.
--
-- Additions only: five new tables and one new column. The only existing
-- object touched is portal_people's role CHECK, widened to also accept
-- 'collector'; no row, column or table is removed.
-- ============================================================

BEGIN;

-- A named group of hospitals, and the times of its two six-hour shifts.
CREATE TABLE IF NOT EXISTS places (
    place_id       SERIAL PRIMARY KEY,
    name           VARCHAR(100) NOT NULL UNIQUE,
    morning_start  TIME NOT NULL DEFAULT '08:00',
    morning_end    TIME NOT NULL DEFAULT '14:00',
    evening_start  TIME NOT NULL DEFAULT '14:00',
    evening_end    TIME NOT NULL DEFAULT '20:00',
    created_by     VARCHAR(254) NOT NULL DEFAULT '',
    created_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS place_hospitals (
    place_id     INTEGER NOT NULL REFERENCES places(place_id),
    hospital_id  VARCHAR(64) NOT NULL REFERENCES hospitals(hospital_id),
    added_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    -- Taken out of the place: the row stays, marked, so nothing is deleted.
    removed_at   TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (place_id, hospital_id)
);

-- A collector's place. Empty for admins and researchers: they see everything.
ALTER TABLE portal_people ADD COLUMN IF NOT EXISTS place_id INTEGER REFERENCES places(place_id);

-- The role list, widened: admin, researcher, and now collector.
DO $$
DECLARE
    c record;
BEGIN
    FOR c IN SELECT conname FROM pg_constraint
              WHERE conrelid = 'portal_people'::regclass AND contype = 'c'
                AND pg_get_constraintdef(oid) LIKE '%role%'
    LOOP
        EXECUTE format('ALTER TABLE portal_people DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;
ALTER TABLE portal_people ADD CONSTRAINT portal_people_role_check
    CHECK (role IN ('admin', 'researcher', 'collector'));

-- Attendance: one row per shift a collector started. Never deleted, times
-- never edited; a shift left open is closed by the server and marked 'auto'.
CREATE TABLE IF NOT EXISTS collector_shifts (
    id          BIGSERIAL PRIMARY KEY,
    email       VARCHAR(254) NOT NULL,
    place_id    INTEGER NOT NULL REFERENCES places(place_id),
    shift       VARCHAR(16) NOT NULL CHECK (shift IN ('morning', 'evening', 'other')),
    shift_date  DATE NOT NULL,
    started_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    ended_at    TIMESTAMP WITH TIME ZONE,
    ended_by    VARCHAR(16) CHECK (ended_by IN ('collector', 'auto')),
    summary_sent_at TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_collector_shifts_open
    ON collector_shifts (place_id) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_collector_shifts_date ON collector_shifts (shift_date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_collector_one_open_shift
    ON collector_shifts (email) WHERE ended_at IS NULL;

-- Every mail the server sent a collector (or tried to): the per-shift log.
CREATE TABLE IF NOT EXISTS collector_notifications (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    email        VARCHAR(254) NOT NULL,
    place_id     INTEGER REFERENCES places(place_id),
    hospital_id  VARCHAR(64),
    device_id    UUID,
    session_id   VARCHAR(64),
    event        VARCHAR(48) NOT NULL,
    urgent       BOOLEAN NOT NULL DEFAULT FALSE,
    subject      VARCHAR(300) NOT NULL,
    body         TEXT NOT NULL,
    happened_at  TIMESTAMP WITH TIME ZONE,
    delivered    BOOLEAN NOT NULL DEFAULT FALSE,
    error        VARCHAR(500),
    seen_at      TIMESTAMP WITH TIME ZONE,
    escalated_at TIMESTAMP WITH TIME ZONE,
    -- What this mail is about, so one event is mailed once to one person.
    event_key    VARCHAR(200) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_collector_notes_email ON collector_notifications (email, id DESC);
CREATE INDEX IF NOT EXISTS idx_collector_notes_key ON collector_notifications (event_key, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_collector_notes_once
    ON collector_notifications (email, event_key);

-- Where the mailer has read up to, so a restart neither repeats nor skips.
-- 'lease' says which API process runs the mailer (several may be running).
CREATE TABLE IF NOT EXISTS collector_notifier_state (
    name        VARCHAR(32) PRIMARY KEY,
    last_id     BIGINT NOT NULL DEFAULT 0,
    owner       VARCHAR(64),
    updated_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

COMMENT ON TABLE places IS 'Named groups of hospitals; a data collector sees only their place''s hospitals.';
COMMENT ON TABLE place_hospitals IS 'Which hospitals belong to which place.';
COMMENT ON COLUMN portal_people.place_id IS 'A data collector''s place; empty for admins and researchers.';
COMMENT ON TABLE collector_shifts IS 'Attendance: every shift a data collector started and ended. Kept for salaries; never deleted.';
COMMENT ON TABLE collector_notifications IS 'Every mail sent to a data collector, and whether it was delivered and seen.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_api') THEN
        GRANT SELECT, INSERT, UPDATE ON places, place_hospitals, collector_shifts,
              collector_notifications, collector_notifier_state TO aims_api;
        GRANT USAGE, SELECT ON SEQUENCE places_place_id_seq, collector_shifts_id_seq,
              collector_notifications_id_seq TO aims_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_monitor') THEN
        GRANT SELECT ON places, place_hospitals, collector_shifts, collector_notifications
              TO aims_monitor;
    END IF;
END $$;

COMMIT;
