-- ================================================================
-- 008 - a register of doctors
--
-- The doctor was moved onto the device enrolment in 006, to stop the browser
-- inventing DR_TEST_001 and DR_DIAG_001 and giving each one a folder in the
-- archive. That fixed the typos and assumed one machine, one doctor.
--
-- It is the wrong assumption. A consulting-room PC is fixed; the doctor using
-- it changes between sessions, days and shifts. Binding the doctor to the
-- machine means re-enrolling hardware every time a rota changes.
--
-- So the two are separated:
--
--   hospital  stays on the device. A PC does not move between hospitals.
--   doctor    comes from CMED per consultation, and is checked against this
--             register for that hospital before a session may open.
--
-- Rotation works, and a doctor who is not credentialed at that hospital is
-- refused - which is the property the device binding was really providing.
--
-- devices.doctor_id is kept as the default for a machine with one regular
-- user, and as the fallback when CMED names nobody.
-- ================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS doctors (
    doctor_id     TEXT NOT NULL,
    hospital_id   TEXT NOT NULL REFERENCES hospitals(hospital_id),
    full_name     TEXT NOT NULL,
    -- Deactivating keeps the history readable: sessions already archived still
    -- resolve to a name, but no new consultation can be opened for them.
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    updated_at    TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    PRIMARY KEY (doctor_id, hospital_id)
);

-- The lookup made on every session open.
CREATE INDEX IF NOT EXISTS idx_doctors_active
    ON doctors(hospital_id, doctor_id) WHERE active;

-- Seed from what the system has already seen, so an existing deployment keeps
-- working without a separate data-entry step. Only doctors that have actually
-- recorded a consultation are taken - a device enrolled to a doctor who never
-- recorded is not evidence that the doctor exists.
INSERT INTO doctors (doctor_id, hospital_id, full_name)
SELECT DISTINCT s.doctor_id, s.hospital_id, s.doctor_id
  FROM sessions s
  JOIN hospitals h ON h.hospital_id = s.hospital_id
 WHERE s.doctor_id IS NOT NULL
   AND s.protocol_version = 2
   AND s.archived_at IS NOT NULL          -- a real, completed consultation
ON CONFLICT (doctor_id, hospital_id) DO NOTHING;

-- Devices carry a default doctor for rooms with one regular user.
INSERT INTO doctors (doctor_id, hospital_id, full_name)
SELECT DISTINCT d.doctor_id, d.hospital_id, d.doctor_id
  FROM devices d
  JOIN hospitals h ON h.hospital_id = d.hospital_id
 WHERE d.doctor_id IS NOT NULL AND d.revoked_at IS NULL
ON CONFLICT (doctor_id, hospital_id) DO NOTHING;


-- Who may record where, for the CMED selector and for review.
CREATE OR REPLACE VIEW v_doctor_register AS
SELECT
    d.hospital_id,
    h.name                AS hospital_name,
    d.doctor_id,
    d.full_name,
    d.active,
    count(s.session_id)   AS consultations,
    max(s.session_date)   AS last_recorded
FROM doctors d
JOIN hospitals h ON h.hospital_id = d.hospital_id
LEFT JOIN sessions s
       ON s.doctor_id = d.doctor_id AND s.hospital_id = d.hospital_id
GROUP BY d.hospital_id, h.name, d.doctor_id, d.full_name, d.active;

COMMIT;
