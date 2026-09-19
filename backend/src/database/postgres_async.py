"""
AIMScribe AI Backend - Async PostgreSQL Database Layer
Uses asyncpg for true async database operations.
"""

import json
import uuid
import logging
from datetime import date, datetime
from typing import Dict, List, Optional, Any

import asyncpg
from asyncpg import Pool

logger = logging.getLogger(__name__)


class AsyncPostgreSQLDatabase:
    """Async PostgreSQL database manager using asyncpg."""

    def __init__(
        self,
        host: str,
        port: int,
        dbname: str,
        user: str,
        password: str,
        min_connections: int = 2,
        max_connections: int = 10,
        sslmode: str = 'prefer'
    ):
        self.dsn = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
        self.min_connections = min_connections
        self.max_connections = max_connections
        self.sslmode = sslmode
        self._pool: Optional[Pool] = None

    async def initialize(self):
        """Initialize the connection pool and create tables."""
        # Configure SSL for cloud databases (Neon, Supabase, etc.)
        ssl_config = True if self.sslmode == 'require' else None

        self._pool = await asyncpg.create_pool(
            self.dsn,
            min_size=self.min_connections,
            max_size=self.max_connections,
            ssl=ssl_config
        )
        await self._init_database()
        logger.info(f"Async PostgreSQL pool initialized (min={self.min_connections}, max={self.max_connections}, ssl={self.sslmode})")

    @property
    def pool(self) -> Optional[Pool]:
        """
        The underlying connection pool.

        Exposed so the v2 integrity layer (src/db_v2.py) can share this pool
        rather than opening a second one - Neon's connection budget is small.
        """
        return self._pool

    async def close(self):
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
            logger.info("Async PostgreSQL pool closed")

    async def healthcheck(self) -> bool:
        """Check if database is healthy."""
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    async def _init_database(self):
        """Initialize database tables."""
        async with self._pool.acquire() as conn:
            # Sessions table - session_id can be client-provided or UUID string
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id VARCHAR(255) PRIMARY KEY,
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
                    ner_webhook_url TEXT,
                    status_webhook_url TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP WITH TIME ZONE
                )
            ''')

            # Add webhook columns if they don't exist (migration for existing DBs)
            await conn.execute('''
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_name='sessions' AND column_name='ner_webhook_url') THEN
                        ALTER TABLE sessions ADD COLUMN ner_webhook_url TEXT;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_name='sessions' AND column_name='status_webhook_url') THEN
                        ALTER TABLE sessions ADD COLUMN status_webhook_url TEXT;
                    END IF;
                END $$;
            ''')

            # Clips table
            await conn.execute('''
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
                )
            ''')

            # Transcripts table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS transcripts (
                    id SERIAL PRIMARY KEY,
                    session_id UUID REFERENCES sessions(session_id) ON DELETE CASCADE,
                    full_transcript TEXT,
                    clip_count INTEGER NOT NULL,
                    last_updated TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # NER results table (individual columns per medical field)
            await conn.execute('''
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
                )
            ''')

            # Doctor reviews table (row-per-change tracking)
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS doctor_reviews (
                    id SERIAL PRIMARY KEY,
                    session_id UUID REFERENCES sessions(session_id) ON DELETE CASCADE,
                    doctor_id VARCHAR(100) NOT NULL,
                    field_name VARCHAR(100) NOT NULL,
                    original_value JSONB,
                    edited_value JSONB,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Prescription data table (saved when doctor presses Print)
            await conn.execute('''
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
                )
            ''')

            # Patients table
            await conn.execute('''
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
            await conn.execute('''
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
            await conn.execute('''
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
                )
            ''')

            # Create indexes
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_sessions_patient ON sessions(patient_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_clips_session ON clips(session_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_ner_session ON ner_results(session_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_doctor_reviews_session ON doctor_reviews(session_id)')
            await conn.execute('CREATE INDEX IF NOT EXISTS idx_prescription_session ON prescription_data(session_id)')

        logger.info("Async PostgreSQL database initialized")

    # ========== Session Operations ==========

    async def create_session(
        self,
        patient_id: str,
        doctor_id: str,
        hospital_id: str,
        session_id: str = None,
        health_screening: dict = None,
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
            health_screening: Health screening data (optional)
            ner_webhook_url: Webhook URL for NER notifications
            status_webhook_url: Webhook URL for status updates
        """
        # Use client-provided session_id or generate UUID
        if not session_id:
            session_id = str(uuid.uuid4())

        async with self._pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO sessions (session_id, patient_id, doctor_id, hospital_id, health_screening, ner_webhook_url, status_webhook_url)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (session_id) DO UPDATE SET
                    patient_id = EXCLUDED.patient_id,
                    doctor_id = EXCLUDED.doctor_id,
                    hospital_id = EXCLUDED.hospital_id,
                    health_screening = COALESCE(EXCLUDED.health_screening, sessions.health_screening),
                    ner_webhook_url = COALESCE(EXCLUDED.ner_webhook_url, sessions.ner_webhook_url),
                    status_webhook_url = COALESCE(EXCLUDED.status_webhook_url, sessions.status_webhook_url),
                    updated_at = CURRENT_TIMESTAMP
            ''', session_id, patient_id, doctor_id, hospital_id,
                json.dumps(health_screening) if health_screening else None,
                ner_webhook_url,
                status_webhook_url)
        return session_id

    async def get_session(self, session_id: str) -> Optional[Dict]:
        """Get session by ID."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM sessions WHERE session_id = $1',
                session_id
            )
            if row:
                result = dict(row)
                # Parse health_screening JSONB
                if isinstance(result.get('health_screening'), str):
                    try:
                        result['health_screening'] = json.loads(result['health_screening'])
                    except (json.JSONDecodeError, TypeError):
                        pass
                return result
            return None

    async def finalize_session(self, session_id: str):
        """Mark session as completed."""
        async with self._pool.acquire() as conn:
            await conn.execute('''
                UPDATE sessions
                SET status = 'completed', completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE session_id = $1
            ''', session_id)

    # ========== Clip Operations ==========

    async def save_clip_record(self, session_id: str, clip_number: int, object_key: str) -> int:
        """Create initial record for a clip."""
        async with self._pool.acquire() as conn:
            result = await conn.fetchval('''
                INSERT INTO clips (session_id, clip_number, object_key, status)
                VALUES ($1, $2, $3, 'pending')
                ON CONFLICT (session_id, clip_number)
                DO UPDATE SET object_key = EXCLUDED.object_key, status = 'pending'
                RETURNING id
            ''', session_id, clip_number, object_key)
            return result

    async def update_clip_status(self, session_id: str, clip_number: int, status: str, error: str = None):
        """Update clip processing status."""
        async with self._pool.acquire() as conn:
            await conn.execute('''
                UPDATE clips
                SET status = $1, error_message = $2
                WHERE session_id = $3 AND clip_number = $4
            ''', status, error, session_id, clip_number)

    async def complete_clip_transcription(
        self,
        session_id: str,
        clip_number: int,
        transcript: str,
        duration: float
    ):
        """Save transcription and update cumulative transcript."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # 1. Update clip record
                await conn.execute('''
                    UPDATE clips
                    SET clip_transcript = $1,
                        audio_duration_seconds = $2,
                        status = 'transcribed',
                        transcribed_at = CURRENT_TIMESTAMP
                    WHERE session_id = $3 AND clip_number = $4
                ''', transcript, duration, session_id, clip_number)

                # 2. Rebuild full transcript from all transcribed clips
                rows = await conn.fetch('''
                    SELECT clip_transcript FROM clips
                    WHERE session_id = $1 AND status = 'transcribed'
                    ORDER BY clip_number ASC
                ''', session_id)

                full_text = "\n\n".join([r['clip_transcript'] for r in rows if r['clip_transcript']])
                count = len(rows)

                # 3. Update sessions table stats
                await conn.execute('''
                    UPDATE sessions
                    SET total_clips = $1,
                        total_duration_seconds = (SELECT COALESCE(SUM(audio_duration_seconds), 0) FROM clips WHERE session_id = $2)
                    WHERE session_id = $2
                ''', count, session_id)

                # 4. Insert new transcript record
                await conn.execute('''
                    INSERT INTO transcripts (session_id, full_transcript, clip_count)
                    VALUES ($1, $2, $3)
                ''', session_id, full_text, count)

    async def get_full_transcript(self, session_id: str) -> Optional[str]:
        """Get latest full transcript."""
        async with self._pool.acquire() as conn:
            result = await conn.fetchval('''
                SELECT full_transcript FROM transcripts
                WHERE session_id = $1 ORDER BY id DESC LIMIT 1
            ''', session_id)
            return result

    async def get_clip_count(self, session_id: str) -> int:
        """Get number of transcribed clips."""
        async with self._pool.acquire() as conn:
            result = await conn.fetchval('''
                SELECT COUNT(*) FROM clips
                WHERE session_id = $1 AND status = 'transcribed'
            ''', session_id)
            return result or 0

    # ========== NER Operations ==========

    # Field mapping: NER JSON key -> database column name
    # NOTE: Examination fields are NESTED under "Examination (English)"
    NER_FIELD_MAP = {
        'Patient Name': 'patient_name',
        'Age': 'age',
        'Gender': 'gender',
        'Chief Complaints (English)': 'chief_complaints',
        'Investigations (English)': 'investigations',
        'Diagnosis (English)': 'diagnosis',
        'Medications': 'medications',
        'Advice (Bengali)': 'advice',
        'Follow Up (Bengali)': 'follow_up',
        'Health Screening': 'health_screening',
    }

    def _decompose_ner_json(self, ner_json: dict) -> dict:
        """Extract individual fields from NER dict for column storage."""
        fields = {}

        # Handle nested Patient Info structure from NER output
        patient_info = ner_json.get('Patient Info (English)', {})
        fields['patient_name'] = patient_info.get('Name (English)') or ner_json.get('Patient Name')
        fields['age'] = patient_info.get('Age (English)') or ner_json.get('Age')
        fields['gender'] = patient_info.get('Gender (English)') or ner_json.get('Gender')

        # Handle top-level fields
        for json_key, col_name in self.NER_FIELD_MAP.items():
            if col_name in ('patient_name', 'age', 'gender'):
                continue  # Already handled above
            value = ner_json.get(json_key)
            # JSONB fields - store as JSON string for asyncpg
            fields[col_name] = json.dumps(value) if value is not None else None

        # Handle NESTED Examination fields under "Examination (English)"
        examination = ner_json.get('Examination (English)', {})

        # O/E (On Examination) - physical exam findings
        on_exam = examination.get('O/E (English)')
        fields['on_examination'] = json.dumps(on_exam) if on_exam else None

        # S/E (Systemic Examination) - organ system findings
        sys_exam = examination.get('S/E (English)')
        fields['systemic_examination'] = json.dumps(sys_exam) if sys_exam else None

        # Drug History - nested under examination
        drug_hist = examination.get('Drug History (English)')
        fields['drug_history'] = json.dumps(drug_hist) if drug_hist else None

        # Additional Notes - nested under examination
        add_notes = examination.get('Additional Notes (English)')
        fields['additional_notes'] = json.dumps(add_notes) if add_notes else None

        return fields

    async def save_ner_result(
        self,
        session_id: str,
        ner_json: dict,
        patient_id: str = None,
        is_final: bool = False,
        method: str = 'langchain_agent',
        prompt_version: str = '1.0.0'
    ) -> int:
        """Save NER extraction result with individual columns."""
        fields = self._decompose_ner_json(ner_json)

        async with self._pool.acquire() as conn:
            # Get next version
            version = await conn.fetchval('''
                SELECT COALESCE(MAX(version), 0) + 1 FROM ner_results WHERE session_id = $1
            ''', session_id)

            result_id = await conn.fetchval('''
                INSERT INTO ner_results (
                    session_id, patient_id, version,
                    patient_name, age, gender,
                    chief_complaints, drug_history, on_examination,
                    systemic_examination, additional_notes, investigations,
                    diagnosis, medications, advice, follow_up, health_screening,
                    full_ner_json, is_final, extraction_method, prompt_version
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21)
                RETURNING id
            ''',
                session_id, patient_id, version,
                fields['patient_name'], fields['age'], fields['gender'],
                fields['chief_complaints'], fields['drug_history'], fields['on_examination'],
                fields['systemic_examination'], fields['additional_notes'], fields['investigations'],
                fields['diagnosis'], fields['medications'], fields['advice'],
                fields['follow_up'], fields['health_screening'],
                json.dumps(ner_json), is_final, method, prompt_version
            )

            return result_id

    async def get_latest_ner(self, session_id: str) -> Optional[Dict]:
        """Get latest NER result with individual columns."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow('''
                SELECT * FROM ner_results
                WHERE session_id = $1 ORDER BY version DESC LIMIT 1
            ''', session_id)
            if row:
                result = dict(row)
                # Parse JSONB fields that asyncpg returns as strings
                for col_name in self.NER_FIELD_MAP.values():
                    if col_name in ('patient_name', 'age', 'gender'):
                        continue
                    val = result.get(col_name)
                    if isinstance(val, str):
                        try:
                            result[col_name] = json.loads(val)
                        except (json.JSONDecodeError, TypeError):
                            pass
                if isinstance(result.get('full_ner_json'), str):
                    result['full_ner_json'] = json.loads(result['full_ner_json'])
                return result
            return None

    # ========== Doctor Review Operations ==========

    async def save_doctor_review(
        self,
        session_id: str,
        doctor_id: str,
        field_name: str,
        original_value: Any,
        edited_value: Any
    ) -> int:
        """Save a single field edit by a doctor."""
        async with self._pool.acquire() as conn:
            result_id = await conn.fetchval('''
                INSERT INTO doctor_reviews (session_id, doctor_id, field_name, original_value, edited_value)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
            ''',
                session_id, doctor_id, field_name,
                json.dumps(original_value), json.dumps(edited_value)
            )
            return result_id

    async def get_doctor_reviews(self, session_id: str) -> List[Dict]:
        """Get all doctor reviews for a session."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch('''
                SELECT * FROM doctor_reviews
                WHERE session_id = $1 ORDER BY created_at ASC
            ''', session_id)
            results = []
            for row in rows:
                r = dict(row)
                for key in ('original_value', 'edited_value'):
                    if isinstance(r.get(key), str):
                        try:
                            r[key] = json.loads(r[key])
                        except (json.JSONDecodeError, TypeError):
                            pass
                results.append(r)
            return results

    # ========== Prescription Operations ==========

    async def save_prescription(
        self,
        session_id: str,
        patient_id: str,
        doctor_id: str,
        prescription: dict
    ) -> int:
        """Save final prescription data (when doctor presses Print)."""
        fields = self._decompose_ner_json(prescription)

        async with self._pool.acquire() as conn:
            result_id = await conn.fetchval('''
                INSERT INTO prescription_data (
                    session_id, patient_id, doctor_id,
                    patient_name, age, gender,
                    chief_complaints, drug_history, on_examination,
                    systemic_examination, additional_notes, investigations,
                    diagnosis, medications, advice, follow_up, health_screening
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
                RETURNING id
            ''',
                session_id, patient_id, doctor_id,
                fields['patient_name'], fields['age'], fields['gender'],
                fields['chief_complaints'], fields['drug_history'], fields['on_examination'],
                fields['systemic_examination'], fields['additional_notes'], fields['investigations'],
                fields['diagnosis'], fields['medications'], fields['advice'],
                fields['follow_up'], fields['health_screening']
            )
            return result_id

    async def get_prescription(self, session_id: str) -> Optional[Dict]:
        """Get latest prescription for a session."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow('''
                SELECT * FROM prescription_data
                WHERE session_id = $1 ORDER BY created_at DESC LIMIT 1
            ''', session_id)
            if row:
                result = dict(row)
                for col_name in self.NER_FIELD_MAP.values():
                    if col_name in ('patient_name', 'age', 'gender'):
                        continue
                    val = result.get(col_name)
                    if isinstance(val, str):
                        try:
                            result[col_name] = json.loads(val)
                        except (json.JSONDecodeError, TypeError):
                            pass
                return result
            return None

    # ========== Patient Operations ==========

    async def get_patient(self, patient_id: str) -> Optional[Dict]:
        """Get patient information."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                'SELECT * FROM patients WHERE patient_id = $1',
                patient_id
            )
            return dict(row) if row else None

    async def upsert_patient(self, patient_data: dict):
        """Insert or update patient."""
        async with self._pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO patients (patient_id, name, age, gender, blood_group, contact, address, medical_history, allergies)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
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
            ''',
                patient_data.get('patient_id'),
                patient_data.get('name'),
                patient_data.get('age'),
                patient_data.get('gender'),
                patient_data.get('blood_group'),
                patient_data.get('contact'),
                patient_data.get('address'),
                json.dumps(patient_data.get('medical_history', {})),
                json.dumps(patient_data.get('allergies', []))
            )

    async def get_patient_baseline(self, patient_id: str) -> Dict:
        """Get patient baseline data (single connection, 3 queries)."""
        baseline = {
            'patient_id': patient_id,
            'demographics': {},
            'health_screening': {},
            'has_previous_visits': False,
            'last_visit_date': None
        }

        async with self._pool.acquire() as conn:
            # Query 1: Get patient
            patient = await conn.fetchrow(
                'SELECT * FROM patients WHERE patient_id = $1',
                patient_id
            )

            # Query 2: Get latest screening
            screening = await conn.fetchrow('''
                SELECT * FROM health_screenings
                WHERE patient_id = $1
                ORDER BY screening_date DESC LIMIT 1
            ''', patient_id)

            # Query 3: Get latest visit date
            visit = await conn.fetchrow('''
                SELECT visit_date FROM previous_visits
                WHERE patient_id = $1
                ORDER BY visit_date DESC LIMIT 1
            ''', patient_id)

        # Process results
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
            }

        if visit:
            baseline['has_previous_visits'] = True
            baseline['last_visit_date'] = str(visit.get('visit_date', ''))

        return baseline

    async def get_last_visit_medications(self, patient_id: str) -> Optional[Dict]:
        """Get medications from patient's most recent visit."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow('''
                SELECT medications, visit_date, diagnosis FROM previous_visits
                WHERE patient_id = $1
                ORDER BY visit_date DESC LIMIT 1
            ''', patient_id)

            if row:
                medications = row.get('medications', [])
                if isinstance(medications, str):
                    try:
                        medications = json.loads(medications)
                    except:
                        medications = []
                return {
                    'medications': medications,
                    'visit_date': row.get('visit_date'),
                    'diagnosis': row.get('diagnosis', [])
                }
            return None

    async def archive_to_previous_visits(
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

        async with self._pool.acquire() as conn:
            result_id = await conn.fetchval('''
                INSERT INTO previous_visits
                (patient_id, session_id, visit_date, chief_complaints, diagnosis,
                 medications, investigations, advice, follow_up, doctor_id, full_ner_json)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING id
            ''',
                patient_id,
                session_id,
                visit_date,
                json.dumps(ner_json.get('Chief Complaints (English)', [])),
                json.dumps(ner_json.get('Diagnosis (English)', [])),
                json.dumps(ner_json.get('Medications', [])),
                json.dumps(ner_json.get('Investigations (English)', [])),
                json.dumps(ner_json.get('Advice (Bengali)', [])),
                json.dumps(ner_json.get('Follow Up (Bengali)', {})),
                doctor_id,
                json.dumps(ner_json)
            )

            logger.info(f"Archived visit {result_id} for patient {patient_id}")
            return result_id
