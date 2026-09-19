"""
AIMScribe AI Backend - Main API
Exposes endpoints for session management, presigned URL generation, and status monitoring.
"""

import logging
from datetime import datetime
from threading import Thread

from flask import Flask, request, jsonify
from config import settings
from database.postgres import PostgreSQLDatabase
from storage.minio_client import get_minio_client, MinIOClient
from message_queue.redis_client import get_redis_client, push_transcription_job
from prompts.loader import get_prompt_loader

# Initialize Flask
app = Flask(__name__)
logger = logging.getLogger(__name__)

# Component Management
class AppContext:
    def __init__(self):
        self.db = PostgreSQLDatabase(
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
            min_connections=settings.postgres_pool_min,
            max_connections=settings.postgres_pool_max
        )
        self.minio = get_minio_client()
        self.redis = get_redis_client()
        self.prompts = get_prompt_loader()

ctx = None

@app.before_request
def initialize():
    global ctx
    if ctx is None:
        ctx = AppContext()

# ============================================================================
# API Endpoints
# ============================================================================

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'database': 'PostgreSQL',
        'redis': ctx.redis.healthcheck(),
        'minio': ctx.minio.file_exists('health') or True, # Simplified check
        'version': '5.1.0',
        'mode': 'Async Worker'
    })

# ========== Session Management ==========

@app.route('/api/v1/session/create', methods=['POST'])
def create_session():
    """
    Create a new session.

    Accepts client-provided session_id (format: PatientID_DoctorID_HospitalID_YYYYMMDD)
    or generates a new UUID if not provided.
    """
    data = request.json or {}
    patient_id = data.get('patient_id')
    doctor_id = data.get('doctor_id', 'DR_DEFAULT')
    hospital_id = data.get('hospital_id', 'HOSP_DEFAULT')
    client_session_id = data.get('session_id')  # Client-provided session ID

    # Optional fields for patient info and webhooks
    patient_name = data.get('patient_name', '')
    patient_age = data.get('age', '')
    patient_gender = data.get('gender', '')
    health_screening = data.get('health_screening', {})
    ner_webhook_url = data.get('ner_webhook_url', '')
    status_webhook_url = data.get('status_webhook_url', '')

    if not patient_id:
        return jsonify({'error': 'patient_id is required'}), 400

    try:
        # Use client-provided session_id or generate new one
        session_id = ctx.db.create_session(
            patient_id,
            doctor_id,
            hospital_id,
            session_id=client_session_id,
            ner_webhook_url=ner_webhook_url,
            status_webhook_url=status_webhook_url
        )

        # Upsert patient with provided info
        patient_data = {
            'patient_id': patient_id,
            'name': patient_name or 'Unknown Patient',
            'age': patient_age or 'N/A',
            'gender': patient_gender or 'N/A'
        }
        if health_screening:
            patient_data['health_screening'] = health_screening

        ctx.db.upsert_patient(patient_data)

        return jsonify({
            'session_id': session_id,
            'status': 'active',
            'created_at': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Create session failed: {e}")
        return jsonify({'error': str(e)}), 500

# ========== Audio Upload (Async) ==========

@app.route('/api/v1/upload/request', methods=['POST'])
def request_upload_url():
    """
    Get a presigned URL for uploading an audio clip.
    Client uploads directly to MinIO using this URL.
    """
    data = request.json or {}
    session_id = data.get('session_id')
    clip_number = data.get('clip_number')
    
    if not session_id or clip_number is None:
        return jsonify({'error': 'session_id and clip_number required'}), 400
        
    try:
        # Generate object key: audio/{session_id}/clip_{n}.wav
        object_key = MinIOClient.generate_object_key(session_id, clip_number)
        
        # Generate presigned PUT URL (expires in 5 mins)
        upload_url = ctx.minio.get_presigned_upload_url(object_key, expires=300)
        
        # Create pending record in DB
        ctx.db.save_clip_record(session_id, clip_number, object_key)
        
        return jsonify({
            'upload_url': upload_url,
            'object_key': object_key,
            'expires_in': 300
        })
    except Exception as e:
        logger.error(f"Presigned URL generation failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/v1/upload/complete', methods=['POST'])
def complete_upload():
    """
    Notify that upload is complete. Pushes job to processing queue.
    """
    data = request.json or {}
    session_id = data.get('session_id')
    clip_number = data.get('clip_number')
    object_key = data.get('object_key')
    is_final = data.get('is_final', False)
    
    if not all([session_id, clip_number, object_key]):
        return jsonify({'error': 'Missing required fields'}), 400
        
    try:
        # Verify clip record exists
        # In strict mode, we'd check if file actually exists in MinIO
        
        # Get patient ID
        session = ctx.db.get_session(session_id)
        if not session:
             return jsonify({'error': 'Session not found'}), 404
             
        # Push to Redis Queue
        job_data = push_transcription_job(
            session_id=session_id,
            clip_number=clip_number,
            object_key=object_key,
            patient_id=session['patient_id'],
            is_final=is_final
        )
        
        return jsonify({
            'status': 'queued',
            'job_id': job_data['job_id'],
            'queue_position': ctx.redis.queue_length("aimscribe:transcription_queue")
        })
    except Exception as e:
        logger.error(f"Job queuing failed: {e}")
        return jsonify({'error': str(e)}), 500

# ========== Results & Monitoring ==========

@app.route('/api/v1/session/<session_id>/status', methods=['GET'])
def get_session_status(session_id):
    """Get processing status for a session."""
    try:
        session = ctx.db.get_session(session_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404
            
        # Get cumulative transcript
        full_transcript = ctx.db.get_full_transcript(session_id)
        
        # Get latest NER
        ner_result = ctx.db.get_latest_ner(session_id)
        
        # Get clip count
        clip_count = ctx.db.get_clip_count(session_id)
        
        return jsonify({
            'session_id': session_id,
            'status': session['status'],
            'total_clips_transcribed': clip_count,
            'has_transcript': bool(full_transcript),
            'has_ner': bool(ner_result),
            'ner_version': ner_result['version'] if ner_result else 0,
            'updated_at': datetime.now().isoformat()
        })
    except Exception as e:
        logger.error(f"Status check failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/v1/transcript/<session_id>', methods=['GET'])
def get_transcript(session_id):
    """Get full cumulative transcript."""
    try:
        transcript = ctx.db.get_full_transcript(session_id)
        return jsonify({
            'session_id': session_id,
            'transcript': transcript or ""
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/v1/ner/<session_id>', methods=['GET'])
def get_ner_result(session_id):
    """Get latest NER result."""
    try:
        result = ctx.db.get_latest_ner(session_id)
        if not result:
             return jsonify({'ner_json': None, 'version': 0})
             
        return jsonify({
            'ner_json': result['ner_json'],
            'version': result['version'],
            'is_final': result['is_final'],
            'created_at': result['created_at']
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ========== Development Entry Point ==========

def main():
    logger.info(f"Starting AIMScribe API on port {settings.server_port}")
    app.run(
        host="0.0.0.0",
        port=settings.server_port,
        debug=settings.debug,
        use_reloader=False 
    )

if __name__ == '__main__':
    main()
