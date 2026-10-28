-- ================================================================
-- 005: every section of a visit as rows, and one place to read them.
--
-- 004 kept API 3's sections (complaints, diagnosis, drug orders, ...) as JSON
-- columns of patient_visit: complete, but every research question became a
-- JSON query. From here each section is a table with one row per item:
--
--   visit_vital            API 2 observations (vitals), with who measured them
--   visit_complaint        API 3 chief_complaints
--   visit_observation      API 3 observations (history, allergy, examination...)
--   visit_diagnosis        API 3 diagnosis, ICD-10 recognised
--   visit_lab_order        API 3 lab_orders
--   visit_radiology_order  API 3 radiology_orders
--   visit_drug_order       API 3 drug_orders
--   visit_advice           API 3 advice
--
-- Every row names the message it came from (message_id -> visit_messages),
-- and every row is a pure function of that message's body: load_visit_sections
-- below is the only code that writes them, live (clinical_v4.file_message calls
-- it) and here for the messages already received. So any of these tables can
-- be rebuilt from visit_messages at any time, and each prescription version
-- keeps its own rows. Which version a visit shows is patient_visit's
-- prescription_message_id / patient_information_message_id.
--
-- Fields this server does not know yet are kept per row in `extra`, never lost.
--
-- Then the research views (v_visit, v_vitals, v_complaints, ..., v_obs_gyne): current
-- version only, with the same names as the recordings database - patient_id,
-- doctor_id, session_id. Researchers read the views; the tables are the store.
--
-- Run as the owner (SET ROLE aims_clinical_owner). Safe to repeat.
-- ================================================================
BEGIN;

-- ---------------------------------------------------------------- helpers
-- Any JSON value as trimmed text; null, missing and "" are NULL.
CREATE OR REPLACE FUNCTION clinical_text(v jsonb, max_len integer DEFAULT 2000)
RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT NULLIF(left(btrim(CASE jsonb_typeof(v)
                                 WHEN 'string' THEN v #>> '{}'
                                 WHEN 'null'   THEN NULL
                                 ELSE v::text END), max_len), '')
$$;

-- A section as its items, numbered from 1: a list as it is, a single value as
-- a list of one, null or missing as nothing.
CREATE OR REPLACE FUNCTION clinical_items(v jsonb)
RETURNS TABLE (ord integer, item jsonb) LANGUAGE sql IMMUTABLE AS $$
    SELECT e.ord::integer, e.item
      FROM jsonb_array_elements(
               CASE WHEN v IS NULL OR jsonb_typeof(v) = 'null' THEN '[]'::jsonb
                    WHEN jsonb_typeof(v) = 'array' THEN v
                    ELSE jsonb_build_array(v) END) WITH ORDINALITY AS e(item, ord)
     WHERE jsonb_typeof(e.item) <> 'null'
$$;

-- An item as an object: a bare value becomes {field: value}.
CREATE OR REPLACE FUNCTION clinical_object(item jsonb, field text)
RETURNS jsonb LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN jsonb_typeof(item) = 'object' THEN item
                ELSE jsonb_build_object(field, item) END
$$;

-- A plain number ("85", "98.5"), else NULL. "128/90" stays text.
CREATE OR REPLACE FUNCTION clinical_number(v text)
RETURNS numeric LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN v ~ '^\s*-?[0-9]{1,9}(\.[0-9]{1,6})?\s*$' THEN btrim(v)::numeric END
$$;

-- A moment with its offset, else NULL; never an error.
CREATE OR REPLACE FUNCTION clinical_moment(v text)
RETURNS timestamptz LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF v IS NULL OR v !~ '^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}' THEN
        RETURN NULL;
    END IF;
    RETURN v::timestamptz;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;

-- ---------------------------------------------------------------- which version a visit shows
ALTER TABLE patient_visit
    ADD COLUMN IF NOT EXISTS patient_information_message_id BIGINT REFERENCES visit_messages (id),
    ADD COLUMN IF NOT EXISTS prescription_message_id        BIGINT REFERENCES visit_messages (id);

-- ---------------------------------------------------------------- the sections
-- Observation-shaped sections (groups of items): group_no is the group's place
-- in the message, item_no the item's place in the group; item_no 0 is the
-- group's own note.
CREATE TABLE IF NOT EXISTS visit_vital (
    message_id   BIGINT  NOT NULL REFERENCES visit_messages (id),
    group_no     INTEGER NOT NULL,
    item_no      INTEGER NOT NULL,
    visit_pk     BIGINT  NOT NULL REFERENCES patient_visit (id),
    category     TEXT,                       -- "vitals" as CMED sends it
    recorded_by  TEXT,                       -- "paramedic", "doctor", or as sent
    recorded_at  TIMESTAMPTZ,
    name         TEXT,                       -- Blood Pressure, Pulse, Weight ...
    value        TEXT,                       -- as written: "128/90", "98.5"
    unit         TEXT,
    value_num    NUMERIC,                    -- the value when it is a plain number
    note         TEXT,
    extra        JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (message_id, group_no, item_no)
);

CREATE TABLE IF NOT EXISTS visit_observation (
    message_id   BIGINT  NOT NULL REFERENCES visit_messages (id),
    group_no     INTEGER NOT NULL,
    item_no      INTEGER NOT NULL,
    visit_pk     BIGINT  NOT NULL REFERENCES patient_visit (id),
    category     TEXT,                       -- personal_history, allergy, on_examination ...
    name         TEXT,                       -- Smoking, Food, Blood Pressure, Heart ...
    value        TEXT,                       -- No, "Yes - prawn", 128/90, NAD
    unit         TEXT,
    value_num    NUMERIC,
    note         TEXT,
    extra        JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (message_id, group_no, item_no)
);

CREATE TABLE IF NOT EXISTS visit_complaint (
    message_id   BIGINT  NOT NULL REFERENCES visit_messages (id),
    line_no      INTEGER NOT NULL,
    visit_pk     BIGINT  NOT NULL REFERENCES patient_visit (id),
    complaint    TEXT,
    duration     TEXT,
    severity     TEXT,
    note         TEXT,
    extra        JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (message_id, line_no)
);

CREATE TABLE IF NOT EXISTS visit_diagnosis (
    message_id   BIGINT  NOT NULL REFERENCES visit_messages (id),
    line_no      INTEGER NOT NULL,
    visit_pk     BIGINT  NOT NULL REFERENCES patient_visit (id),
    code         TEXT,                       -- as sent
    display      TEXT,
    type         TEXT,                       -- provisional, final ...
    note         TEXT,
    is_icd10     BOOLEAN GENERATED ALWAYS AS
                     (code ~ '^[A-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$') STORED,
    extra        JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (message_id, line_no)
);

CREATE TABLE IF NOT EXISTS visit_lab_order (
    message_id   BIGINT  NOT NULL REFERENCES visit_messages (id),
    line_no      INTEGER NOT NULL,
    visit_pk     BIGINT  NOT NULL REFERENCES patient_visit (id),
    order_id     TEXT,
    test_code    TEXT,
    test_name    TEXT,
    priority     TEXT,
    status       TEXT,
    extra        JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (message_id, line_no)
);

CREATE TABLE IF NOT EXISTS visit_radiology_order (
    message_id     BIGINT  NOT NULL REFERENCES visit_messages (id),
    line_no        INTEGER NOT NULL,
    visit_pk       BIGINT  NOT NULL REFERENCES patient_visit (id),
    order_id       TEXT,
    procedure_code TEXT,
    procedure_name TEXT,
    body_site      TEXT,
    status         TEXT,
    extra          JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (message_id, line_no)
);

CREATE TABLE IF NOT EXISTS visit_drug_order (
    message_id      BIGINT  NOT NULL REFERENCES visit_messages (id),
    line_no         INTEGER NOT NULL,
    visit_pk        BIGINT  NOT NULL REFERENCES patient_visit (id),
    order_id        TEXT,
    medication_code TEXT,
    form            TEXT,                    -- Tablet, Capsule, Suspension ...
    medication_name TEXT,                    -- the brand: Napa One
    generic_name    TEXT,                    -- Paracetamol
    strength        TEXT,
    dose            TEXT,                    -- 1+1+1, 2 spoon
    route           TEXT,
    frequency       TEXT,
    timing          TEXT,                    -- after meal
    duration        TEXT,
    instruction     TEXT,
    status          TEXT,
    extra           JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (message_id, line_no)
);

CREATE TABLE IF NOT EXISTS visit_advice (
    message_id   BIGINT  NOT NULL REFERENCES visit_messages (id),
    line_no      INTEGER NOT NULL,
    visit_pk     BIGINT  NOT NULL REFERENCES patient_visit (id),
    type         TEXT,                       -- general, diet, medication, warning ...
    instruction  TEXT,
    extra        JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (message_id, line_no)
);

CREATE INDEX IF NOT EXISTS idx_visit_vital_visit       ON visit_vital (visit_pk);
CREATE INDEX IF NOT EXISTS idx_visit_observation_visit ON visit_observation (visit_pk);
CREATE INDEX IF NOT EXISTS idx_visit_complaint_visit   ON visit_complaint (visit_pk);
CREATE INDEX IF NOT EXISTS idx_visit_diagnosis_visit   ON visit_diagnosis (visit_pk);
CREATE INDEX IF NOT EXISTS idx_visit_diagnosis_code    ON visit_diagnosis (code);
CREATE INDEX IF NOT EXISTS idx_visit_lab_order_visit   ON visit_lab_order (visit_pk);
CREATE INDEX IF NOT EXISTS idx_visit_radiology_visit   ON visit_radiology_order (visit_pk);
CREATE INDEX IF NOT EXISTS idx_visit_drug_order_visit  ON visit_drug_order (visit_pk);
CREATE INDEX IF NOT EXISTS idx_visit_drug_order_generic ON visit_drug_order (lower(generic_name));
CREATE INDEX IF NOT EXISTS idx_visit_advice_visit      ON visit_advice (visit_pk);

-- ---------------------------------------------------------------- the one loader
-- One message's sections as rows. Repeating it changes nothing: the rows are
-- a function of the message, and a message never changes.
CREATE OR REPLACE FUNCTION load_visit_sections(p_message BIGINT)
RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE
    m      visit_messages%ROWTYPE;
    v_pk   BIGINT;
    total  INTEGER := 0;
    n      INTEGER;
BEGIN
    SELECT * INTO m FROM visit_messages WHERE id = p_message;
    IF NOT FOUND THEN
        RETURN 0;
    END IF;
    SELECT id INTO v_pk FROM patient_visit
     WHERE hospital_id = m.hospital_id AND visit_id = m.visit_id;
    IF v_pk IS NULL THEN
        RETURN 0;
    END IF;

    IF m.kind = 'patient_information' THEN
        INSERT INTO visit_vital (message_id, group_no, item_no, visit_pk, category, recorded_by,
                                 recorded_at, name, value, unit, value_num, note, extra)
        SELECT m.id, g.ord, i.ord, v_pk,
               clinical_text(go->'category', 64), clinical_text(go->'recorded_by', 64),
               clinical_moment(clinical_text(go->'recorded_at', 64)),
               clinical_text(io->'name', 200), clinical_text(io->'value', 500),
               clinical_text(io->'unit', 64), clinical_number(clinical_text(io->'value', 64)),
               clinical_text(io->'note', 2000), io - ARRAY['name', 'value', 'unit', 'note']
          FROM clinical_items(m.body->'observations') g
          CROSS JOIN LATERAL clinical_object(g.item, 'category') go
          CROSS JOIN LATERAL clinical_items(go->'items') i
          CROSS JOIN LATERAL clinical_object(i.item, 'name') io
        ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
        INSERT INTO visit_vital (message_id, group_no, item_no, visit_pk, category, recorded_by,
                                 recorded_at, note, extra)
        SELECT m.id, g.ord, 0, v_pk, clinical_text(go->'category', 64),
               clinical_text(go->'recorded_by', 64),
               clinical_moment(clinical_text(go->'recorded_at', 64)),
               clinical_text(go->'note', 2000),
               go - ARRAY['category', 'recorded_by', 'recorded_at', 'items', 'note']
          FROM clinical_items(m.body->'observations') g
          CROSS JOIN LATERAL clinical_object(g.item, 'category') go
         WHERE clinical_text(go->'note') IS NOT NULL
        ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
        RETURN total;
    END IF;

    -- A prescription (API 3).
    INSERT INTO visit_complaint (message_id, line_no, visit_pk, complaint, duration, severity,
                                 note, extra)
    SELECT m.id, s.ord, v_pk, clinical_text(o->'complaint', 500), clinical_text(o->'duration', 200),
           clinical_text(o->'severity', 64), clinical_text(o->'note', 2000),
           o - ARRAY['complaint', 'duration', 'severity', 'note']
      FROM clinical_items(m.body->'chief_complaints') s
      CROSS JOIN LATERAL clinical_object(s.item, 'complaint') o
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT; total := total + n;

    INSERT INTO visit_observation (message_id, group_no, item_no, visit_pk, category, name, value,
                                   unit, value_num, note, extra)
    SELECT m.id, g.ord, i.ord, v_pk, clinical_text(go->'category', 64),
           clinical_text(io->'name', 200), clinical_text(io->'value', 2000),
           clinical_text(io->'unit', 64), clinical_number(clinical_text(io->'value', 64)),
           clinical_text(io->'note', 2000), io - ARRAY['name', 'value', 'unit', 'note']
      FROM clinical_items(m.body->'observations') g
      CROSS JOIN LATERAL clinical_object(g.item, 'category') go
      CROSS JOIN LATERAL clinical_items(go->'items') i
      CROSS JOIN LATERAL clinical_object(i.item, 'name') io
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
    INSERT INTO visit_observation (message_id, group_no, item_no, visit_pk, category, note, extra)
    SELECT m.id, g.ord, 0, v_pk, clinical_text(go->'category', 64), clinical_text(go->'note', 4000),
           go - ARRAY['category', 'items', 'note']
      FROM clinical_items(m.body->'observations') g
      CROSS JOIN LATERAL clinical_object(g.item, 'category') go
     WHERE clinical_text(go->'note') IS NOT NULL
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT; total := total + n;

    INSERT INTO visit_diagnosis (message_id, line_no, visit_pk, code, display, type, note, extra)
    SELECT m.id, s.ord, v_pk, upper(clinical_text(o->'code', 32)), clinical_text(o->'display', 500),
           clinical_text(o->'type', 64), clinical_text(o->'note', 2000),
           o - ARRAY['code', 'display', 'type', 'note']
      FROM clinical_items(m.body->'diagnosis') s
      CROSS JOIN LATERAL clinical_object(s.item, 'display') o
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT; total := total + n;

    INSERT INTO visit_lab_order (message_id, line_no, visit_pk, order_id, test_code, test_name,
                                 priority, status, extra)
    SELECT m.id, s.ord, v_pk, clinical_text(o->'order_id', 100), clinical_text(o->'test_code', 100),
           clinical_text(o->'test_name', 500), clinical_text(o->'priority', 64),
           clinical_text(o->'status', 64),
           o - ARRAY['order_id', 'test_code', 'test_name', 'priority', 'status']
      FROM clinical_items(m.body->'lab_orders') s
      CROSS JOIN LATERAL clinical_object(s.item, 'test_name') o
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT; total := total + n;

    INSERT INTO visit_radiology_order (message_id, line_no, visit_pk, order_id, procedure_code,
                                       procedure_name, body_site, status, extra)
    SELECT m.id, s.ord, v_pk, clinical_text(o->'order_id', 100),
           clinical_text(o->'procedure_code', 100), clinical_text(o->'procedure_name', 500),
           clinical_text(o->'body_site', 200), clinical_text(o->'status', 64),
           o - ARRAY['order_id', 'procedure_code', 'procedure_name', 'body_site', 'status']
      FROM clinical_items(m.body->'radiology_orders') s
      CROSS JOIN LATERAL clinical_object(s.item, 'procedure_name') o
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT; total := total + n;

    INSERT INTO visit_drug_order (message_id, line_no, visit_pk, order_id, medication_code, form,
                                  medication_name, generic_name, strength, dose, route, frequency,
                                  timing, duration, instruction, status, extra)
    SELECT m.id, s.ord, v_pk, clinical_text(o->'order_id', 100),
           clinical_text(o->'medication_code', 100), clinical_text(o->'form', 100),
           clinical_text(o->'medication_name', 300), clinical_text(o->'generic_name', 300),
           clinical_text(o->'strength', 100), clinical_text(o->'dose', 200),
           clinical_text(o->'route', 64), clinical_text(o->'frequency', 200),
           clinical_text(o->'timing', 200), clinical_text(o->'duration', 200),
           clinical_text(o->'instruction', 2000), clinical_text(o->'status', 64),
           o - ARRAY['order_id', 'medication_code', 'form', 'medication_name', 'generic_name',
                     'strength', 'dose', 'route', 'frequency', 'timing', 'duration',
                     'instruction', 'status']
      FROM clinical_items(m.body->'drug_orders') s
      CROSS JOIN LATERAL clinical_object(s.item, 'medication_name') o
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT; total := total + n;

    INSERT INTO visit_advice (message_id, line_no, visit_pk, type, instruction, extra)
    SELECT m.id, s.ord, v_pk, clinical_text(o->'type', 64), clinical_text(o->'instruction', 4000),
           o - ARRAY['type', 'instruction']
      FROM clinical_items(m.body->'advice') s
      CROSS JOIN LATERAL clinical_object(s.item, 'instruction') o
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS n = ROW_COUNT; total := total + n;
    RETURN total;
END $$;

-- ---------------------------------------------------------------- the messages already here
-- Every message's rows, then which version each visit shows, decided as
-- clinical_v4 decides it: the latest API 2; the prescription with the latest
-- issued_at (one without issued_at, or with the same, replaces the one held).
DO $$
DECLARE
    r        RECORD;
    held     BIGINT;
    held_at  TIMESTAMPTZ;
    v        RECORD;
BEGIN
    FOR r IN SELECT id FROM visit_messages ORDER BY id LOOP
        PERFORM load_visit_sections(r.id);
    END LOOP;

    UPDATE patient_visit pv
       SET patient_information_message_id = (
               SELECT max(id) FROM visit_messages vm
                WHERE vm.kind = 'patient_information' AND vm.hospital_id = pv.hospital_id
                  AND vm.visit_id = pv.visit_id)
     WHERE pv.patient_information_message_id IS NULL;

    FOR v IN SELECT id, hospital_id, visit_id FROM patient_visit
              WHERE prescription_message_id IS NULL LOOP
        held := NULL; held_at := NULL;
        FOR r IN SELECT id, clinical_moment(body->>'issued_at') AS issued
                   FROM visit_messages
                  WHERE kind = 'prescription' AND hospital_id = v.hospital_id
                    AND visit_id = v.visit_id
                  ORDER BY id LOOP
            IF held IS NULL OR held_at IS NULL OR r.issued IS NULL OR r.issued >= held_at THEN
                held := r.id; held_at := r.issued;
            END IF;
        END LOOP;
        IF held IS NOT NULL THEN
            UPDATE patient_visit SET prescription_message_id = held WHERE id = v.id;
        END IF;
    END LOOP;
END $$;

-- ---------------------------------------------------------------- the research views
-- One row per visit: who, when, the recording, and what has arrived. The
-- names match the recordings database: patient_id, doctor_id, session_id.
CREATE OR REPLACE VIEW v_visit AS
SELECT pv.hospital_id,
       pv.visit_id,
       pv.hospital_patient_id                 AS patient_id,
       pv.practitioner_id                     AS doctor_id,
       pv.aims_id                             AS session_id,
       pv.visit_date,
       pv.start_time,
       p.sex,
       p.date_of_birth,
       pv.age,
       pv.age_years,
       p.blood_group,
       pv.patient_information_at IS NOT NULL  AS has_patient_information,
       pv.prescription_at IS NOT NULL         AS has_prescription,
       pv.aims_id IS NOT NULL                 AS has_recording,
       pv.prescription_version                AS prescription_versions,
       pv.issued_at                           AS prescription_issued_at,
       pv.follow_up,
       pv.follow_up_text,
       (SELECT count(*) FROM visit_complaint x WHERE x.message_id = pv.prescription_message_id)
                                              AS complaints,
       (SELECT count(*) FROM visit_diagnosis x WHERE x.message_id = pv.prescription_message_id)
                                              AS diagnoses,
       (SELECT count(*) FROM visit_drug_order x WHERE x.message_id = pv.prescription_message_id)
                                              AS drug_orders,
       (SELECT count(*) FROM visit_lab_order x WHERE x.message_id = pv.prescription_message_id)
                                              AS lab_orders,
       (SELECT count(*) FROM visit_radiology_order x
         WHERE x.message_id = pv.prescription_message_id)
                                              AS radiology_orders,
       pv.patient_information_at,
       pv.prescription_at
  FROM patient_visit pv
  JOIN patient p ON p.hospital_id = pv.hospital_id
                AND p.hospital_patient_id = pv.hospital_patient_id;

-- The visit's identifiers, repeated on every section view so each one stands
-- alone in a query or a spreadsheet.
CREATE OR REPLACE VIEW v_vitals AS
SELECT pv.hospital_id, pv.visit_id, pv.hospital_patient_id AS patient_id,
       pv.practitioner_id AS doctor_id, pv.aims_id AS session_id, pv.visit_date,
       'patient_information'::text                 AS source,
       COALESCE(x.recorded_by, 'paramedic')        AS measured_by,
       x.recorded_at                               AS measured_at,
       x.name, x.value, x.unit, x.value_num,
       CASE WHEN x.value ~ '^\s*\d{2,3}\s*/\s*\d{2,3}\s*$'
            THEN split_part(regexp_replace(x.value, '\s', '', 'g'), '/', 1)::integer END
                                                   AS systolic,
       CASE WHEN x.value ~ '^\s*\d{2,3}\s*/\s*\d{2,3}\s*$'
            THEN split_part(regexp_replace(x.value, '\s', '', 'g'), '/', 2)::integer END
                                                   AS diastolic
  FROM patient_visit pv
  JOIN visit_vital x ON x.message_id = pv.patient_information_message_id AND x.item_no > 0
UNION ALL
-- The doctor's own measurements, written under "on examination" in API 3.
SELECT pv.hospital_id, pv.visit_id, pv.hospital_patient_id, pv.practitioner_id, pv.aims_id,
       pv.visit_date, 'prescription', 'doctor', pv.issued_at,
       x.name, x.value, x.unit, x.value_num,
       CASE WHEN x.value ~ '^\s*\d{2,3}\s*/\s*\d{2,3}\s*$'
            THEN split_part(regexp_replace(x.value, '\s', '', 'g'), '/', 1)::integer END,
       CASE WHEN x.value ~ '^\s*\d{2,3}\s*/\s*\d{2,3}\s*$'
            THEN split_part(regexp_replace(x.value, '\s', '', 'g'), '/', 2)::integer END
  FROM patient_visit pv
  JOIN visit_observation x ON x.message_id = pv.prescription_message_id AND x.item_no > 0
 WHERE lower(x.category) = 'on_examination'
   AND lower(x.name) IN ('blood pressure', 'bp', 'pulse', 'pulse rate', 'heart rate',
                         'temperature', 'temp', 'respiratory rate', 'weight', 'height', 'bmi',
                         'spo2', 'oxygen saturation');

CREATE OR REPLACE VIEW v_complaints AS
SELECT pv.hospital_id, pv.visit_id, pv.hospital_patient_id AS patient_id,
       pv.practitioner_id AS doctor_id, pv.aims_id AS session_id, pv.visit_date,
       x.line_no, x.complaint, x.duration, x.severity, x.note
  FROM patient_visit pv
  JOIN visit_complaint x ON x.message_id = pv.prescription_message_id;

CREATE OR REPLACE VIEW v_observations AS
SELECT pv.hospital_id, pv.visit_id, pv.hospital_patient_id AS patient_id,
       pv.practitioner_id AS doctor_id, pv.aims_id AS session_id, pv.visit_date,
       x.group_no, x.item_no, x.category, x.name, x.value, x.unit, x.value_num, x.note
  FROM patient_visit pv
  JOIN visit_observation x ON x.message_id = pv.prescription_message_id;

CREATE OR REPLACE VIEW v_diagnoses AS
SELECT pv.hospital_id, pv.visit_id, pv.hospital_patient_id AS patient_id,
       pv.practitioner_id AS doctor_id, pv.aims_id AS session_id, pv.visit_date,
       x.line_no, x.code, x.is_icd10, x.display, x.type, x.note
  FROM patient_visit pv
  JOIN visit_diagnosis x ON x.message_id = pv.prescription_message_id;

CREATE OR REPLACE VIEW v_lab_orders AS
SELECT pv.hospital_id, pv.visit_id, pv.hospital_patient_id AS patient_id,
       pv.practitioner_id AS doctor_id, pv.aims_id AS session_id, pv.visit_date,
       x.line_no, x.order_id, x.test_code, x.test_name, x.priority, x.status
  FROM patient_visit pv
  JOIN visit_lab_order x ON x.message_id = pv.prescription_message_id;

CREATE OR REPLACE VIEW v_radiology_orders AS
SELECT pv.hospital_id, pv.visit_id, pv.hospital_patient_id AS patient_id,
       pv.practitioner_id AS doctor_id, pv.aims_id AS session_id, pv.visit_date,
       x.line_no, x.order_id, x.procedure_code, x.procedure_name, x.body_site, x.status
  FROM patient_visit pv
  JOIN visit_radiology_order x ON x.message_id = pv.prescription_message_id;

CREATE OR REPLACE VIEW v_drug_orders AS
SELECT pv.hospital_id, pv.visit_id, pv.hospital_patient_id AS patient_id,
       pv.practitioner_id AS doctor_id, pv.aims_id AS session_id, pv.visit_date,
       x.line_no, x.order_id, x.medication_code, x.form, x.medication_name, x.generic_name,
       x.strength, x.dose, x.route, x.frequency, x.timing, x.duration, x.instruction, x.status
  FROM patient_visit pv
  JOIN visit_drug_order x ON x.message_id = pv.prescription_message_id;

CREATE OR REPLACE VIEW v_advice AS
SELECT pv.hospital_id, pv.visit_id, pv.hospital_patient_id AS patient_id,
       pv.practitioner_id AS doctor_id, pv.aims_id AS session_id, pv.visit_date,
       x.line_no, x.type, x.instruction
  FROM patient_visit pv
  JOIN visit_advice x ON x.message_id = pv.prescription_message_id;

-- Obstetric and gynaecological history: one row per visit that has any, one
-- column per item. Only visits CMED sent this section for appear - so a male
-- patient never shows a row of empty cells, and an empty cell here means one
-- thing: this woman's prescription did not include that item. Sex is shown,
-- never used to filter: gynae data for a patient recorded as male is a data
-- error to find (sex_mismatch), not to hide. Items under other names are kept
-- in other_items.
CREATE OR REPLACE FUNCTION clinical_day(v text)
RETURNS date LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF v IS NULL OR v !~ '^\s*\d{4}-\d{2}-\d{2}\s*$' THEN
        RETURN NULL;
    END IF;
    RETURN btrim(v)::date;
EXCEPTION WHEN others THEN
    RETURN NULL;
END $$;

CREATE OR REPLACE VIEW v_obs_gyne AS
WITH items AS (
    SELECT pv.id AS visit_pk, lower(x.category) AS category, x.name, x.value, x.note,
           x.item_no,
           btrim(regexp_replace(lower(COALESCE(x.name, '')), '[^a-z0-9]+', ' ', 'g')) AS k
      FROM patient_visit pv
      JOIN visit_observation x ON x.message_id = pv.prescription_message_id
     WHERE lower(x.category) IN ('obs_gyne', 'anc', 'pnc')
)
SELECT pv.hospital_id, pv.visit_id, pv.hospital_patient_id AS patient_id,
       pv.practitioner_id AS doctor_id, pv.aims_id AS session_id, pv.visit_date,
       p.sex,
       COALESCE(p.sex, '') = 'male'                                        AS sex_mismatch,
       max(i.value) FILTER (WHERE i.k IN ('age of menarche', 'menarche'))  AS age_of_menarche,
       max(i.value) FILTER (WHERE i.k IN ('cycle', 'menstrual cycle'))     AS cycle,
       max(i.value) FILTER (WHERE i.k IN ('amount of flow', 'flow'))       AS amount_of_flow,
       max(i.value) FILTER (WHERE i.k IN ('days of period', 'duration of period'))
                                                                           AS days_of_period,
       max(i.value) FILTER (WHERE i.k IN ('lmp', 'last menstrual period')) AS lmp,
       clinical_day(max(i.value) FILTER (WHERE i.k IN ('lmp', 'last menstrual period')))
                                                                           AS lmp_date,
       max(i.value) FILTER (WHERE i.k = 'gravida')                         AS gravida,
       max(i.value) FILTER (WHERE i.k = 'para')                            AS para,
       max(i.value) FILTER (WHERE i.k = 'age of last child')               AS age_of_last_child,
       max(i.value) FILTER (WHERE i.k IN ('delivery history', 'mode of delivery'))
                                                                           AS delivery_history,
       max(i.value) FILTER (WHERE i.k IN ('contraceptive history', 'contraception'))
                                                                           AS contraceptive_history,
       bool_or(i.category = 'anc')                                         AS has_anc,
       bool_or(i.category = 'pnc')                                         AS has_pnc,
       jsonb_object_agg(i.category || ': ' || i.name, i.value) FILTER (
           WHERE i.item_no > 0 AND i.name IS NOT NULL AND (i.category <> 'obs_gyne' OR i.k NOT IN (
               'age of menarche', 'menarche', 'cycle', 'menstrual cycle', 'amount of flow',
               'flow', 'days of period', 'duration of period', 'lmp', 'last menstrual period',
               'gravida', 'para', 'age of last child', 'delivery history', 'mode of delivery',
               'contraceptive history', 'contraception')))                AS other_items,
       string_agg(i.note, ' | ') FILTER (WHERE i.item_no = 0)              AS note
  FROM items i
  JOIN patient_visit pv ON pv.id = i.visit_pk
  JOIN patient p ON p.hospital_id = pv.hospital_id
                AND p.hospital_patient_id = pv.hospital_patient_id
 GROUP BY pv.id, pv.hospital_id, pv.visit_id, pv.hospital_patient_id, pv.practitioner_id,
          pv.aims_id, pv.visit_date, p.sex;

COMMENT ON VIEW v_obs_gyne IS
    'Obstetric and gynaecological history, one row per visit that has it; sex shown, never filtered.';

-- Every prescription version received, and which one the visit shows.
CREATE OR REPLACE VIEW v_prescription_versions AS
SELECT vm.hospital_id, vm.visit_id, pv.hospital_patient_id AS patient_id,
       vm.version, clinical_moment(vm.body->>'issued_at') AS issued_at, vm.received_at,
       vm.id = pv.prescription_message_id                AS shown,
       vm.id                                             AS message_id
  FROM visit_messages vm
  JOIN patient_visit pv ON pv.hospital_id = vm.hospital_id AND pv.visit_id = vm.visit_id
 WHERE vm.kind = 'prescription';

COMMENT ON TABLE visit_vital IS
    'API 2 vitals, one row per item; rebuildable from visit_messages by load_visit_sections.';
COMMENT ON TABLE visit_drug_order IS
    'API 3 drug orders, one row per drug, per prescription version (message_id).';
COMMENT ON VIEW v_visit IS
    'One row per visit: start here. Names match the recordings database.';
COMMENT ON VIEW v_vitals IS
    'Vitals as measured by the paramedic (API 2) and by the doctor (API 3, on examination).';

-- ---------------------------------------------------------------- who may do what
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_clinical_writer') THEN
        -- Rows are added, never changed: a message never changes.
        GRANT SELECT, INSERT ON visit_vital, visit_observation, visit_complaint,
              visit_diagnosis, visit_lab_order, visit_radiology_order, visit_drug_order,
              visit_advice TO aims_clinical_writer;
        GRANT SELECT ON v_visit, v_vitals, v_complaints, v_observations, v_diagnoses,
              v_lab_orders, v_radiology_orders, v_drug_orders, v_advice,
              v_prescription_versions, v_obs_gyne TO aims_clinical_writer;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aims_clinical_reader') THEN
        GRANT SELECT ON visit_vital, visit_observation, visit_complaint, visit_diagnosis,
              visit_lab_order, visit_radiology_order, visit_drug_order, visit_advice,
              v_visit, v_vitals, v_complaints, v_observations, v_diagnoses, v_lab_orders,
              v_radiology_orders, v_drug_orders, v_advice, v_prescription_versions, v_obs_gyne
              TO aims_clinical_reader;
    END IF;
END $$;

COMMIT;
