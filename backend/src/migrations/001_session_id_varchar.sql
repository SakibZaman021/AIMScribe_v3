-- Migration: Convert session_id from UUID to VARCHAR(255)
-- This allows client-provided session IDs in format: PatientID_DoctorID_HospitalID_YYYYMMDD

-- Step 1: Drop foreign key constraints
ALTER TABLE clips DROP CONSTRAINT IF EXISTS clips_session_id_fkey;
ALTER TABLE transcripts DROP CONSTRAINT IF EXISTS transcripts_session_id_fkey;
ALTER TABLE ner_results DROP CONSTRAINT IF EXISTS ner_results_session_id_fkey;
ALTER TABLE doctor_edits DROP CONSTRAINT IF EXISTS doctor_edits_session_id_fkey;

-- Step 2: Alter session_id columns to VARCHAR(255)
ALTER TABLE sessions ALTER COLUMN session_id TYPE VARCHAR(255) USING session_id::text;
ALTER TABLE clips ALTER COLUMN session_id TYPE VARCHAR(255) USING session_id::text;
ALTER TABLE transcripts ALTER COLUMN session_id TYPE VARCHAR(255) USING session_id::text;
ALTER TABLE ner_results ALTER COLUMN session_id TYPE VARCHAR(255) USING session_id::text;
ALTER TABLE doctor_edits ALTER COLUMN session_id TYPE VARCHAR(255) USING session_id::text;
ALTER TABLE previous_visits ALTER COLUMN session_id TYPE VARCHAR(255) USING session_id::text;

-- Step 3: Re-add foreign key constraints
ALTER TABLE clips ADD CONSTRAINT clips_session_id_fkey
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE;
ALTER TABLE transcripts ADD CONSTRAINT transcripts_session_id_fkey
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE;
ALTER TABLE ner_results ADD CONSTRAINT ner_results_session_id_fkey
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE;
ALTER TABLE doctor_edits ADD CONSTRAINT doctor_edits_session_id_fkey
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE;

-- Step 4: Add webhook columns if not exist
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS ner_webhook_url TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS status_webhook_url TEXT;

-- Step 5: Add idempotency_key column to clips
ALTER TABLE clips ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(64);

-- Verify
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name = 'sessions' AND column_name = 'session_id';
