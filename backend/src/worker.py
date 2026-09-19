"""
AIMScribe AI Backend - Async Worker
Consumes jobs from Redis queue and runs processing pipeline.
"""

import os
import sys
import time
import logging
import json
import traceback

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from message_queue.redis_client import get_redis_client, TRANSCRIPTION_QUEUE, DEAD_LETTER_QUEUE
from storage.minio_client import get_minio_client
from database.postgres import PostgreSQLDatabase
from config import settings
from processing.transcriber_v4 import TranscriberV4
from processing.ner_extractor import NERExtractor

# Check for ptvsd for debugging (optional)
try:
    import ptvsd
    # ptvsd.enable_attach()
except ImportError:
    pass

# Configure Logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format='%(asctime)s - WORKER - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class AIMScribeWorker:
    """
    Async worker for AIMScribe.
    """
    
    def __init__(self):
        logger.info("Initializing AIMScribe Worker...")
        
        # Clients
        self.redis = get_redis_client()
        self.minio = get_minio_client()
        
        # Database (with connection pooling)
        self.db = PostgreSQLDatabase(
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
            min_connections=settings.postgres_pool_min,
            max_connections=settings.postgres_pool_max
        )
        
        # Processing Modules
        # Using GPT-4o-transcribe-diarize via Audio Transcriptions API
        self.transcriber = TranscriberV4()
        self.ner_extractor = NERExtractor()
        
        # State
        self.running = True
        
    def run(self):
        """Main worker loop."""
        logger.info(f"Worker started. Listening on {TRANSCRIPTION_QUEUE}...")
        
        while self.running:
            try:
                # Blocking pop with 5 second timeout
                job = self.redis.pop_job(TRANSCRIPTION_QUEUE, timeout=5)
                
                if job:
                    self.process_job(job)
                
                # Small sleep to prevent tight loop if redis fails
                # time.sleep(0.1) 
                
            except KeyboardInterrupt:
                logger.info("Worker stopping...")
                self.running = False
            except Exception as e:
                logger.error(f"Unexpected error in worker loop: {e}")
                time.sleep(5)  # Backoff

    def process_job(self, job: dict):
        """Process a single job with guaranteed temp file cleanup."""
        start_time = time.time()
        job_id = job.get('job_id')
        session_id = job.get('session_id')
        clip_number = job.get('clip_number')
        object_key = job.get('object_key')

        # Define temp file path upfront for guaranteed cleanup
        temp_file = f"/tmp/{session_id}_{clip_number}.wav"

        logger.info(f"Processing job {job_id} (Session: {session_id}, Clip: {clip_number})")

        try:
            # 1. Update Status
            self.redis.set_job_status(session_id, clip_number, 'processing')
            self.db.update_clip_status(session_id, clip_number, 'transcribing')

            # 2. Download Audio
            self.minio.download_file(object_key, temp_file)

            # 3. Transcribe
            transcript = self.transcriber.transcribe(temp_file, session_id)
            duration = 0.0  # TODO: Get actual duration using pydub/ffmpeg if needed

            # 4. Update Database (Clip + Cumulative)
            self.db.complete_clip_transcription(
                session_id,
                clip_number,
                transcript,
                duration
            )

            # 4.1 Invalidate session context cache (transcript changed)
            self.redis.invalidate_session_context(session_id)

            # 5. Check NER Trigger
            clip_count = self.db.get_clip_count(session_id)
            is_final = job.get('is_final', False)

            if clip_count >= settings.ner_trigger_clips or is_final:
                self._run_ner_extraction(session_id, is_final)
            else:
                logger.info(f"Skipping NER for session {session_id} (Clips: {clip_count}/{settings.ner_trigger_clips})")

            # 6. Update Status
            self.redis.set_job_status(session_id, clip_number, 'completed')
            logger.info(f"Job {job_id} completed in {time.time() - start_time:.2f}s")

        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}")
            logger.error(traceback.format_exc())
            self.db.update_clip_status(session_id, clip_number, 'failed', str(e))
            self.redis.set_job_status(session_id, clip_number, 'failed', {'error': str(e)})
            self.redis.move_to_dead_letter(job, str(e))

        finally:
            # GUARANTEED cleanup - runs whether success or failure
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                    logger.debug(f"Cleaned up temp file: {temp_file}")
                except OSError as cleanup_error:
                    logger.warning(f"Failed to cleanup temp file {temp_file}: {cleanup_error}")

    def _run_ner_extraction(self, session_id: str, is_final: bool):
        """Run NER extraction pipeline with Redis caching."""
        logger.info(f"Triggering NER for session {session_id} (Final: {is_final})")

        # Get full transcript
        full_transcript = self.db.get_full_transcript(session_id)
        if not full_transcript:
            logger.warning("No transcript found for NER")
            return

        # Get session to retrieve patient_id
        session = self.db.get_session(session_id)
        patient_id = session['patient_id']

        # Get patient baseline (check cache first)
        baseline = self.redis.get_cached_patient_baseline(patient_id)
        if not baseline:
            logger.debug(f"Fetching patient baseline from DB: {patient_id}")
            baseline = self.db.get_patient_baseline(patient_id)
            # Cache for future use (1 hour TTL)
            self.redis.cache_patient_baseline(patient_id, baseline, ttl=3600)

        # Get previous medications (check cache first)
        prev_meds = self.redis.get_cached_previous_medications(patient_id)
        if prev_meds is None:
            logger.debug(f"Fetching previous medications from DB: {patient_id}")
            prev_meds = self.db.get_last_visit_medications(patient_id)
            if prev_meds:
                self.redis.cache_previous_medications(patient_id, prev_meds, ttl=3600)

        # Run Extraction (now parallelized)
        ner_result = self.ner_extractor.extract_all(
            full_transcript,
            patient_context=baseline,
            previous_medications=prev_meds['medications'] if prev_meds else None
        )

        # Save Result
        self.db.save_ner_result(session_id, ner_result, is_final=is_final)

        # If final, archive and invalidate caches
        if is_final:
            self.db.finalize_session(session_id)
            self.db.archive_to_previous_visits(
                session_id=session_id,
                patient_id=patient_id,
                doctor_id=session.get('doctor_id', 'AUTO'),
                ner_json=ner_result
            )
            # Invalidate caches since patient history changed
            self.redis.invalidate_patient_baseline(patient_id)
            self.redis.invalidate_session_context(session_id)
            logger.info(f"Session {session_id} finalized and archived")


if __name__ == "__main__":
    worker = AIMScribeWorker()
    worker.run()
