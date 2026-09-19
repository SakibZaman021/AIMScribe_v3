"""
AIMScribe AI Backend - PostgreSQL Database Layer
Handles all database operations for sessions, transcripts, NER results, and patient data.
Uses connection pooling for efficient database access.
"""

import json
import uuid
import logging
from datetime import date, datetime
from typing import Dict, List, Optional, Any
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor, Json

logger = logging.getLogger(__name__)


class PostgreSQLDatabase:
    """PostgreSQL database manager for AIMScribe with connection pooling."""

    def __init__(
        self,
        host: str,
        port: int,
        dbname: str,
        user: str,
        password: str,
        sslmode: str = 'prefer',
        min_connections: int = 2,
        max_connections: int = 10
    ):
        self.connection_params = {
            'host': host,
            'port': port,
            'dbname': dbname,
            'user': user,
            'password': password,
            'sslmode': sslmode
        }
        self.min_connections = min_connections
        self.max_connections = max_connections
        self._pool = None
        self._init_pool()
        self._init_database()

    def _init_pool(self):
        """Initialize the connection pool."""
        try:
            self._pool = pool.ThreadedConnectionPool(
                minconn=self.min_connections,
                maxconn=self.max_connections,
                **self.connection_params
            )
            logger.info(f"Connection pool initialized (min={self.min_connections}, max={self.max_connections})")
        except Exception as e:
            logger.error(f"Failed to initialize connection pool: {e}")
            raise

    @contextmanager
    def get_connection(self):
        """Context manager for database connections from pool."""
        conn = None
        try:
            conn = self._pool.getconn()
            yield conn
            conn.commit()
        except Exception as e:
            if conn:
                conn.rollback()
            raise e
        finally:
            if conn:
                self._pool.putconn(conn)

    def close_pool(self):
        """Close all connections in the pool."""
        if self._pool:
            self._pool.closeall()
            logger.info("Connection pool closed")
    
    def _init_database(self):
        """Initialize database tables."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                
                # Sessions table - session_id can be client-provided string or UUID
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id VARCHAR(255) PRIMARY KEY,
                        patient_id VARCHAR(100) NOT NULL,
                        doctor_id VARCHAR(100) NOT NULL,
                        hospital_id VARCHAR(100) NOT NULL,
                        status VARCHAR(20) DEFAULT 'active',
                        total_clips INTEGER DEFAULT 0,
                        total_duration_seconds DECIMAL(10,2) DEFAULT 0,
                        ner_webhook_url TEXT,
                        status_webhook_url TEXT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMP WITH TIME ZONE
                    )
                ''')
                
                # Clips table (tracks individual audio clips)
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS clips (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(255) REFERENCES sessions(session_id) ON DELETE CASCADE,
                        clip_number INTEGER NOT NULL,
                        object_key VARCHAR(255) NOT NULL,
                        clip_transcript TEXT,
                        status VARCHAR(20) DEFAULT 'pending',
                        audio_duration_seconds DECIMAL(10,2),
                        idempotency_key VARCHAR(64),
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        transcribed_at TIMESTAMP WITH TIME ZONE,
                        error_message TEXT,
                        UNIQUE(session_id, clip_number)
                    )
                ''')

                # Transcripts table (cumulative full transcript)
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS transcripts (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(255) REFERENCES sessions(session_id) ON DELETE CASCADE,
                        full_transcript TEXT,
                        clip_count INTEGER NOT NULL,
                        last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # NER results table with JSONB
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS ner_results (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(255) REFERENCES sessions(session_id) ON DELETE CASCADE,
                        version INTEGER NOT NULL,
                        ner_json JSONB NOT NULL,
                        is_final BOOLEAN DEFAULT FALSE,
                        extraction_method VARCHAR(50) DEFAULT 'langchain_agent',
                        prompt_version VARCHAR(20) DEFAULT '1.0.0',
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Doctor edits tracking
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS doctor_edits (
                        id SERIAL PRIMARY KEY,
                        session_id VARCHAR(255) REFERENCES sessions(session_id) ON DELETE CASCADE,
                        ner_version INTEGER NOT NULL,
                        field_path VARCHAR(255) NOT NULL,
                        original_value TEXT,
                        edited_value TEXT,
                        doctor_id VARCHAR(100) NOT NULL,
                        edit_reason TEXT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Patients table
                cur.execute('''
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
                    )
                ''')
                
                # Health screenings table
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS health_screenings (
                        id SERIAL PRIMARY KEY,
                        patient_id VARCHAR(100) REFERENCES patients(patient_id) ON DELETE CASCADE,
                        screening_date DATE NOT NULL,
                        screening_type VARCHAR(100),
                        results JSONB,
                        notes TEXT,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Previous visits table
                cur.execute('''
                    CREATE TABLE IF NOT EXISTS previous_visits (
                        id SERIAL PRIMARY KEY,
                        patient_id VARCHAR(100) REFERENCES patients(patient_id) ON DELETE CASCADE,
                        session_id VARCHAR(255),
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
                    )
                ''')
                
                # Create indexes
                cur.execute('CREATE INDEX IF NOT EXISTS idx_sessions_patient ON sessions(patient_id)')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_sessions_doctor ON sessions(doctor_id)')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_transcripts_session ON transcripts(session_id)')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_ner_session ON ner_results(session_id)')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_edits_session ON doctor_edits(session_id)')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_screenings_patient ON health_screenings(patient_id)')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_visits_patient ON previous_visits(patient_id)')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_visits_date ON previous_visits(visit_date)')
                
                # GIN indexes for JSONB
                cur.execute('CREATE INDEX IF NOT EXISTS idx_ner_json ON ner_results USING GIN(ner_json)')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_patient_history ON patients USING GIN(medical_history)')
                cur.execute('CREATE INDEX IF NOT EXISTS idx_visits_medications ON previous_visits USING GIN(medications)')
                
        logger.info("PostgreSQL database initialized successfully")
    
    # ========== Session Operations ==========
    
    def create_session(
        self,
        patient_id: str,
        doctor_id: str,
        hospital_id: str,
        session_id: str = None,
        ner_webhook_url: str = None,
        status_webhook_url: str = None
    ) -> str:
        """
        Create a new recording session.

        Args:
            patient_id: Patient identifier
            doctor_id: Doctor identifier
            hospital_id: Hospital identifier
            session_id: Client-provided session ID (format: PatientID_DoctorID_HospitalID_YYYYMMDD)
                       If not provided, generates UUID.
            ner_webhook_url: Webhook URL for NER result notifications
            status_webhook_url: Webhook URL for status updates
        """
        # Use client-provided session_id or generate UUID
        if not session_id:
            session_id = str(uuid.uuid4())

        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO sessions (
                        session_id, patient_id, doctor_id, hospital_id,
                        ner_webhook_url, status_webhook_url
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (session_id) DO UPDATE SET
                        patient_id = EXCLUDED.patient_id,
                        doctor_id = EXCLUDED.doctor_id,
                        hospital_id = EXCLUDED.hospital_id,
                        ner_webhook_url = COALESCE(EXCLUDED.ner_webhook_url, sessions.ner_webhook_url),
                        status_webhook_url = COALESCE(EXCLUDED.status_webhook_url, sessions.status_webhook_url),
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING session_id
                ''', (session_id, patient_id, doctor_id, hospital_id, ner_webhook_url, status_webhook_url))
                result = cur.fetchone()
        return str(result[0])
    
    def get_session(self, session_id: str) -> Optional[Dict]:
        """Get session by ID."""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('SELECT * FROM sessions WHERE session_id = %s', (session_id,))
                return cur.fetchone()
    
    def update_session(self, session_id: str, **kwargs):
        """Update session fields."""
        if not kwargs:
            return
        
        set_clause = ', '.join([f"{k} = %s" for k in kwargs.keys()])
        values = list(kwargs.values()) + [session_id]
        
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f'''
                    UPDATE sessions 
                    SET {set_clause}, updated_at = CURRENT_TIMESTAMP 
                    WHERE session_id = %s
                ''', values)
    
    def finalize_session(self, session_id: str):
        """Mark session as completed."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    UPDATE sessions 
                    SET status = 'completed', completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = %s
                ''', (session_id,))
    
    # ========== Clip & Transcript Operations ==========
    
    def save_clip_record(
        self,
        session_id: str,
        clip_number: int,
        object_key: str
    ) -> int:
        """Create initial record for a clip."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO clips (session_id, clip_number, object_key, status)
                    VALUES (%s, %s, %s, 'pending')
                    ON CONFLICT (session_id, clip_number) 
                    DO UPDATE SET object_key = EXCLUDED.object_key, status = 'pending'
                    RETURNING id
                ''', (session_id, clip_number, object_key))
                return cur.fetchone()[0]
    
    def update_clip_status(
        self,
        session_id: str,
        clip_number: int,
        status: str,
        error: str = None
    ):
        """Update clip processing status."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    UPDATE clips 
                    SET status = %s, error_message = %s
                    WHERE session_id = %s AND clip_number = %s
                ''', (status, error, session_id, clip_number))

    def complete_clip_transcription(
        self,
        session_id: str,
        clip_number: int,
        transcript: str,
        duration: float
    ):
        """
        Save transcription validation and update cumulative transcript.
        Transactional update to ensure consistency.
        """
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                # 1. Update clip record
                cur.execute('''
                    UPDATE clips 
                    SET clip_transcript = %s, 
                        audio_duration_seconds = %s, 
                        status = 'transcribed',
                        transcribed_at = CURRENT_TIMESTAMP
                    WHERE session_id = %s AND clip_number = %s
                ''', (transcript, duration, session_id, clip_number))
                
                # 2. Rebuild full transcript from all transcribed clips
                cur.execute('''
                    SELECT clip_transcript FROM clips 
                    WHERE session_id = %s AND status = 'transcribed'
                    ORDER BY clip_number ASC
                ''', (session_id,))
                rows = cur.fetchall()
                full_text = "\n\n".join([r[0] for r in rows if r[0]])
                count = len(rows)
                
                # 3. Update sessions table stats
                cur.execute('''
                    UPDATE sessions 
                    SET total_clips = %s, 
                        total_duration_seconds = (SELECT SUM(audio_duration_seconds) FROM clips WHERE session_id = %s)
                    WHERE session_id = %s
                ''', (count, session_id, session_id))
                
                # 4. Update/Insert transcripts table
                cur.execute('''
                    INSERT INTO transcripts (session_id, full_transcript, clip_count)
                    VALUES (%s, %s, %s)
                ''', (session_id, full_text, count))

    def get_full_transcript(self, session_id: str) -> Optional[str]:
        """Get latest full transcript."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT full_transcript FROM transcripts 
                    WHERE session_id = %s ORDER BY id DESC LIMIT 1
                ''', (session_id,))
                result = cur.fetchone()
                return result[0] if result else None
                
    def get_clip_count(self, session_id: str) -> int:
        """Get number of transcribed clips."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT COUNT(*) FROM clips 
                    WHERE session_id = %s AND status = 'transcribed'
                ''', (session_id,))
                return cur.fetchone()[0]
    
    # ========== NER Operations ==========
    
    def save_ner_result(
        self,
        session_id: str,
        ner_json: dict,
        is_final: bool = False,
        method: str = 'langchain_agent',
        prompt_version: str = '1.0.0'
    ) -> int:
        """Save NER extraction result."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    SELECT COALESCE(MAX(version), 0) + 1 FROM ner_results WHERE session_id = %s
                ''', (session_id,))
                version = cur.fetchone()[0]
                
                cur.execute('''
                    INSERT INTO ner_results (session_id, version, ner_json, is_final, extraction_method, prompt_version)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (session_id, version, Json(ner_json), is_final, method, prompt_version))
                
                return cur.fetchone()[0]
    
    def get_latest_ner(self, session_id: str) -> Optional[Dict]:
        """Get latest NER result."""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('''
                    SELECT * FROM ner_results 
                    WHERE session_id = %s ORDER BY version DESC LIMIT 1
                ''', (session_id,))
                return cur.fetchone()
    
    # ========== Patient Operations ==========
    
    def get_patient(self, patient_id: str) -> Optional[Dict]:
        """Get patient information."""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('SELECT * FROM patients WHERE patient_id = %s', (patient_id,))
                return cur.fetchone()
    
    def upsert_patient(self, patient_data: dict):
        """Insert or update patient."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO patients (patient_id, name, age, gender, blood_group, contact, address, medical_history, allergies)
                    VALUES (%(patient_id)s, %(name)s, %(age)s, %(gender)s, %(blood_group)s, %(contact)s, %(address)s, %(medical_history)s, %(allergies)s)
                    ON CONFLICT (patient_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        age = EXCLUDED.age,
                        gender = EXCLUDED.gender,
                        blood_group = EXCLUDED.blood_group,
                        contact = EXCLUDED.contact,
                        address = EXCLUDED.address,
                        medical_history = EXCLUDED.medical_history,
                        allergies = EXCLUDED.allergies,
                        updated_at = CURRENT_TIMESTAMP
                ''', {
                    'patient_id': patient_data.get('patient_id'),
                    'name': patient_data.get('name'),
                    'age': patient_data.get('age'),
                    'gender': patient_data.get('gender'),
                    'blood_group': patient_data.get('blood_group'),
                    'contact': patient_data.get('contact'),
                    'address': patient_data.get('address'),
                    'medical_history': Json(patient_data.get('medical_history', {})),
                    'allergies': Json(patient_data.get('allergies', []))
                })
    
    def get_patient_history(self, patient_id: str, limit: int = 5) -> List[Dict]:
        """Get patient's previous visits."""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('''
                    SELECT * FROM previous_visits 
                    WHERE patient_id = %s 
                    ORDER BY visit_date DESC LIMIT %s
                ''', (patient_id, limit))
                return cur.fetchall()
    
    def get_health_screenings(self, patient_id: str, limit: int = 5) -> List[Dict]:
        """Get patient's health screenings."""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('''
                    SELECT * FROM health_screenings 
                    WHERE patient_id = %s 
                    ORDER BY screening_date DESC LIMIT %s
                ''', (patient_id, limit))
                return cur.fetchall()
    
    def get_patient_baseline(self, patient_id: str) -> Dict:
        """
        Get patient baseline data (demographics + health screening + visit history).
        Optimized: Uses single connection with multiple queries instead of 3 separate connections.
        """
        baseline = {
            'patient_id': patient_id,
            'demographics': {},
            'health_screening': {},
            'has_previous_visits': False,
            'last_visit_date': None
        }

        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Query 1: Get patient demographics
                cur.execute('SELECT * FROM patients WHERE patient_id = %s', (patient_id,))
                patient = cur.fetchone()

                # Query 2: Get latest health screening
                cur.execute('''
                    SELECT * FROM health_screenings
                    WHERE patient_id = %s
                    ORDER BY screening_date DESC LIMIT 1
                ''', (patient_id,))
                screening = cur.fetchone()

                # Query 3: Get latest visit date
                cur.execute('''
                    SELECT visit_date FROM previous_visits
                    WHERE patient_id = %s
                    ORDER BY visit_date DESC LIMIT 1
                ''', (patient_id,))
                visit = cur.fetchone()

        # Process patient demographics
        if patient:
            baseline['demographics'] = {
                'name': patient.get('name', 'N/A'),
                'age': patient.get('age', 'N/A'),
                'gender': patient.get('gender', 'N/A'),
                'blood_group': patient.get('blood_group', 'N/A'),
                'contact': patient.get('contact', 'N/A'),
                'address': patient.get('address', 'N/A'),
                'allergies': patient.get('allergies', []),
                'medical_history': patient.get('medical_history', {})
            }

        # Process health screening
        if screening:
            results = screening.get('results', {})
            if isinstance(results, str):
                try:
                    results = json.loads(results)
                except:
                    results = {}

            baseline['health_screening'] = {
                'screening_date': str(screening.get('screening_date', 'N/A')),
                'diabetes': results.get('diabetes', 'N/A'),
                'hypertension': results.get('hypertension', 'N/A'),
                'smoking': results.get('smoking', 'N/A'),
                'alcohol': results.get('alcohol', 'N/A'),
                'cardiac_disease': results.get('cardiac_disease', 'N/A'),
                'kidney_disease': results.get('kidney_disease', 'N/A'),
                'liver_disease': results.get('liver_disease', 'N/A'),
                'thyroid': results.get('thyroid', 'N/A')
            }

        # Process visit history
        if visit:
            baseline['has_previous_visits'] = True
            baseline['last_visit_date'] = str(visit.get('visit_date', ''))

        return baseline
    
    def get_last_visit_medications(self, patient_id: str) -> Optional[Dict]:
        """Get medications from patient's most recent visit."""
        with self.get_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute('''
                    SELECT medications, visit_date, diagnosis FROM previous_visits 
                    WHERE patient_id = %s 
                    ORDER BY visit_date DESC LIMIT 1
                ''', (patient_id,))
                result = cur.fetchone()
                
                if result:
                    medications = result.get('medications', [])
                    if isinstance(medications, str):
                        try:
                            medications = json.loads(medications)
                        except:
                            medications = []
                    return {
                        'medications': medications,
                        'visit_date': result.get('visit_date'),
                        'diagnosis': result.get('diagnosis', [])
                    }
                return None
    
    def archive_to_previous_visits(
        self,
        session_id: str,
        patient_id: str,
        doctor_id: str,
        ner_json: dict,
        visit_date: date = None
    ) -> int:
        """Archive completed visit to previous_visits table."""
        if visit_date is None:
            visit_date = date.today()
        
        chief_complaints = ner_json.get('Chief Complaints (English)', [])
        diagnosis = ner_json.get('Diagnosis (English)', [])
        medications = ner_json.get('Medications', [])
        investigations = ner_json.get('Investigations (English)', [])
        advice = ner_json.get('Advice (Bengali)', [])
        follow_up = ner_json.get('Follow Up (Bengali)', {})
        
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    INSERT INTO previous_visits 
                    (patient_id, session_id, visit_date, chief_complaints, diagnosis, 
                     medications, investigations, advice, follow_up, doctor_id, full_ner_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                ''', (
                    patient_id,
                    session_id,
                    visit_date,
                    Json(chief_complaints),
                    Json(diagnosis),
                    Json(medications),
                    Json(investigations),
                    Json(advice),
                    Json(follow_up),
                    doctor_id,
                    Json(ner_json)
                ))
                
                visit_id = cur.fetchone()[0]
                logger.info(f"Archived visit {visit_id} for patient {patient_id}")
                return visit_id
