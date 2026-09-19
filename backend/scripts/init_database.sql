-- ================================================================
-- AIMScribe Database Schema
-- Matches postgres_async.py _init_database() exactly
-- ================================================================

-- ================================================================
-- SESSIONS TABLE
-- ================================================================
CREATE TABLE IF NOT EXISTS sessions (
    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id VARCHAR(100) NOT NULL,
    doctor_id VARCHAR(100) NOT NULL,
    hospital_id VARCHAR(100) NOT NULL,
    status VARCHAR(20) DEFAULT 'active',
    total_clips INTEGER DEFAULT 0,
    total_duration_seconds DECIMAL(10,2) DEFAULT 0,
    health_screening JSONB,
    recording_date DATE,
    start_time TIME,
    end_time TIME,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE
);

-- ================================================================
-- CLIPS TABLE
-- ================================================================
CREATE TABLE IF NOT EXISTS clips (
    id SERIAL PRIMARY KEY,
    session_id UUID REFERENCES sessions(session_id) ON DELETE CASCADE,
    clip_number INTEGER NOT NULL,
    object_key VARCHAR(255) NOT NULL,
    clip_transcript TEXT,
    status VARCHAR(20) DEFAULT 'pending',
    audio_duration_seconds DECIMAL(10,2),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    transcribed_at TIMESTAMP WITH TIME ZONE,
    error_message TEXT,
    UNIQUE(session_id, clip_number)
);

-- ================================================================
-- TRANSCRIPTS TABLE
-- ================================================================
CREATE TABLE IF NOT EXISTS transcripts (
    id SERIAL PRIMARY KEY,
    session_id UUID REFERENCES sessions(session_id) ON DELETE CASCADE,
    full_transcript TEXT,
    clip_count INTEGER NOT NULL,
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ================================================================
-- NER RESULTS TABLE (Individual columns per medical field)
-- ================================================================
CREATE TABLE IF NOT EXISTS ner_results (
    id SERIAL PRIMARY KEY,
    session_id UUID REFERENCES sessions(session_id) ON DELETE CASCADE,
    patient_id VARCHAR(100),
    version INTEGER NOT NULL,
    patient_name TEXT,
    age TEXT,
    gender TEXT,
    chief_complaints JSONB,
    drug_history JSONB,
    on_examination JSONB,
    systemic_examination JSONB,
    additional_notes JSONB,
    investigations JSONB,
    diagnosis JSONB,
    medications JSONB,
    advice JSONB,
    follow_up JSONB,
    health_screening JSONB,
    full_ner_json JSONB NOT NULL,
    is_final BOOLEAN DEFAULT FALSE,
    extraction_method VARCHAR(50) DEFAULT 'langchain_agent',
    prompt_version VARCHAR(20) DEFAULT '1.0.0',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ================================================================
-- DOCTOR REVIEWS TABLE (row-per-change tracking)
-- ================================================================
CREATE TABLE IF NOT EXISTS doctor_reviews (
    id SERIAL PRIMARY KEY,
    session_id UUID REFERENCES sessions(session_id) ON DELETE CASCADE,
    doctor_id VARCHAR(100) NOT NULL,
    field_name VARCHAR(100) NOT NULL,
    original_value JSONB,
    edited_value JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ================================================================
-- PRESCRIPTION DATA TABLE (saved when doctor presses Print)
-- ================================================================
CREATE TABLE IF NOT EXISTS prescription_data (
    id SERIAL PRIMARY KEY,
    session_id UUID REFERENCES sessions(session_id) ON DELETE CASCADE,
    patient_id VARCHAR(100),
    doctor_id VARCHAR(100) NOT NULL,
    patient_name TEXT,
    age TEXT,
    gender TEXT,
    chief_complaints JSONB,
    drug_history JSONB,
    on_examination JSONB,
    systemic_examination JSONB,
    additional_notes JSONB,
    investigations JSONB,
    diagnosis JSONB,
    medications JSONB,
    advice JSONB,
    follow_up JSONB,
    health_screening JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ================================================================
-- PATIENTS TABLE
-- ================================================================
CREATE TABLE IF NOT EXISTS patients (
    patient_id VARCHAR(100) PRIMARY KEY,
    name VARCHAR(255),
    age VARCHAR(20),
    gender VARCHAR(20),
    blood_group VARCHAR(10),
    contact VARCHAR(50),
    address TEXT,
    medical_history JSONB,
    allergies JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ================================================================
-- HEALTH SCREENINGS TABLE
-- ================================================================
CREATE TABLE IF NOT EXISTS health_screenings (
    id SERIAL PRIMARY KEY,
    patient_id VARCHAR(100) REFERENCES patients(patient_id) ON DELETE CASCADE,
    screening_date DATE NOT NULL,
    screening_type VARCHAR(100),
    results JSONB,
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ================================================================
-- PREVIOUS VISITS TABLE
-- ================================================================
CREATE TABLE IF NOT EXISTS previous_visits (
    id SERIAL PRIMARY KEY,
    patient_id VARCHAR(100) REFERENCES patients(patient_id) ON DELETE CASCADE,
    session_id UUID,
    visit_date DATE NOT NULL,
    chief_complaints JSONB,
    diagnosis JSONB,
    medications JSONB,
    investigations JSONB,
    advice JSONB,
    follow_up JSONB,
    doctor_id VARCHAR(100),
    doctor_notes TEXT,
    full_ner_json JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ================================================================
-- INDEXES
-- ================================================================
CREATE INDEX IF NOT EXISTS idx_sessions_patient ON sessions(patient_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_clips_session ON clips(session_id);
CREATE INDEX IF NOT EXISTS idx_ner_session ON ner_results(session_id);
CREATE INDEX IF NOT EXISTS idx_doctor_reviews_session ON doctor_reviews(session_id);
CREATE INDEX IF NOT EXISTS idx_prescription_session ON prescription_data(session_id);

-- ================================================================
-- TABLE COMMENTS
-- ================================================================
COMMENT ON TABLE sessions IS 'Doctor-patient consultation sessions';
COMMENT ON TABLE clips IS 'Individual audio clips with transcripts';
COMMENT ON TABLE transcripts IS 'Cumulative transcripts per session';
COMMENT ON TABLE ner_results IS 'Extracted medical entities from transcripts (individual columns)';
COMMENT ON TABLE doctor_reviews IS 'Doctor edits to NER fields (row-per-change audit trail)';
COMMENT ON TABLE prescription_data IS 'Final prescription saved when doctor presses Print';
COMMENT ON TABLE patients IS 'Patient demographic information';
COMMENT ON TABLE health_screenings IS 'Patient health screening data';
COMMENT ON TABLE previous_visits IS 'Archived sessions for follow-up reference';
