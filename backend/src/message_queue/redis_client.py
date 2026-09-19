"""
AIMScribe AI Backend - Redis Client
Message queue for async job processing.
"""

import json
import logging
from typing import Optional, Dict, Any, Callable
from datetime import datetime

import redis

logger = logging.getLogger(__name__)


# Queue names
TRANSCRIPTION_QUEUE = "aimscribe:transcription_queue"
NER_QUEUE = "aimscribe:ner_queue"
DEAD_LETTER_QUEUE = "aimscribe:dead_letter_queue"


class RedisClient:
    """
    Redis client for message queuing.
    
    Features:
    - Push/pop messages from queues
    - Blocking pop for workers
    - Job status tracking
    - Dead letter queue for failed jobs
    """
    
    def __init__(
        self,
        host: str,
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None
    ):
        """
        Initialize Redis client.
        
        Args:
            host: Redis server host
            port: Redis server port
            db: Redis database number
            password: Optional password
        """
        self.client = redis.Redis(
            host=host,
            port=port,
            db=db,
            password=password,
            decode_responses=True
        )
        
        # Test connection
        self.client.ping()
        logger.info(f"Redis client connected: {host}:{port}")
    
    # ========== Queue Operations ==========
    
    def push_job(self, queue: str, job_data: Dict[str, Any]) -> bool:
        """
        Push a job to a queue.
        
        Args:
            queue: Queue name
            job_data: Job data dictionary
            
        Returns:
            True if successful
        """
        job_data['queued_at'] = datetime.now().isoformat()
        message = json.dumps(job_data)
        self.client.lpush(queue, message)
        logger.debug(f"Pushed job to {queue}: {job_data.get('job_id', 'unknown')}")
        return True
    
    def pop_job(self, queue: str, timeout: int = 0) -> Optional[Dict[str, Any]]:
        """
        Pop a job from a queue (blocking).
        
        Args:
            queue: Queue name
            timeout: Blocking timeout in seconds (0 = forever)
            
        Returns:
            Job data dictionary or None
        """
        result = self.client.brpop(queue, timeout=timeout)
        if result:
            _, message = result
            job_data = json.loads(message)
            logger.debug(f"Popped job from {queue}: {job_data.get('job_id', 'unknown')}")
            return job_data
        return None
    
    def pop_job_non_blocking(self, queue: str) -> Optional[Dict[str, Any]]:
        """
        Pop a job from a queue (non-blocking).
        
        Args:
            queue: Queue name
            
        Returns:
            Job data dictionary or None
        """
        message = self.client.rpop(queue)
        if message:
            return json.loads(message)
        return None
    
    def queue_length(self, queue: str) -> int:
        """Get the number of jobs in a queue."""
        return self.client.llen(queue)
    
    def move_to_dead_letter(self, job_data: Dict[str, Any], error: str):
        """
        Move a failed job to dead letter queue.
        
        Args:
            job_data: Original job data
            error: Error message
        """
        job_data['error'] = error
        job_data['failed_at'] = datetime.now().isoformat()
        self.push_job(DEAD_LETTER_QUEUE, job_data)
        logger.warning(f"Moved job to dead letter queue: {job_data.get('job_id', 'unknown')}")
    
    # ========== Job Status Tracking ==========
    
    def set_job_status(
        self,
        session_id: str,
        clip_number: int,
        status: str,
        details: Dict[str, Any] = None
    ):
        """
        Set the status of a processing job.
        
        Args:
            session_id: Session ID
            clip_number: Clip number
            status: Status string (pending, processing, completed, failed)
            details: Additional details
        """
        key = f"aimscribe:job_status:{session_id}:{clip_number}"
        value = {
            'status': status,
            'updated_at': datetime.now().isoformat(),
            **(details or {})
        }
        self.client.setex(key, 3600 * 24, json.dumps(value))  # TTL: 24 hours
    
    def get_job_status(self, session_id: str, clip_number: int) -> Optional[Dict]:
        """Get the status of a processing job."""
        key = f"aimscribe:job_status:{session_id}:{clip_number}"
        value = self.client.get(key)
        if value:
            return json.loads(value)
        return None
    
    # ========== Session Status ==========
    
    def set_session_processing_status(
        self,
        session_id: str,
        status: str,
        clip_count: int = 0,
        ner_version: int = 0
    ):
        """
        Set overall session processing status.
        
        Args:
            session_id: Session ID
            status: Status string
            clip_count: Number of clips processed
            ner_version: Current NER version
        """
        key = f"aimscribe:session_status:{session_id}"
        value = {
            'status': status,
            'clip_count': clip_count,
            'ner_version': ner_version,
            'updated_at': datetime.now().isoformat()
        }
        self.client.setex(key, 3600 * 24, json.dumps(value))
    
    def get_session_processing_status(self, session_id: str) -> Optional[Dict]:
        """Get session processing status."""
        key = f"aimscribe:session_status:{session_id}"
        value = self.client.get(key)
        if value:
            return json.loads(value)
        return None
    
    # ========== Pub/Sub for Real-time Updates ==========
    
    def publish_update(self, session_id: str, update_type: str, data: Dict):
        """
        Publish a real-time update for a session.
        
        Args:
            session_id: Session ID
            update_type: Type of update (transcript, ner, status)
            data: Update data
        """
        channel = f"aimscribe:updates:{session_id}"
        message = {
            'type': update_type,
            'data': data,
            'timestamp': datetime.now().isoformat()
        }
        self.client.publish(channel, json.dumps(message))
    
    def subscribe_updates(self, session_id: str) -> redis.client.PubSub:
        """
        Subscribe to updates for a session.
        
        Args:
            session_id: Session ID
            
        Returns:
            PubSub object for listening
        """
        channel = f"aimscribe:updates:{session_id}"
        pubsub = self.client.pubsub()
        pubsub.subscribe(channel)
        return pubsub
    
    # ========== Caching for Performance ==========

    def cache_patient_baseline(self, patient_id: str, baseline: Dict[str, Any], ttl: int = 3600):
        """
        Cache patient baseline data.

        Args:
            patient_id: Patient ID
            baseline: Baseline data dictionary
            ttl: Time-to-live in seconds (default: 1 hour)
        """
        key = f"aimscribe:cache:patient_baseline:{patient_id}"
        self.client.setex(key, ttl, json.dumps(baseline, default=str))
        logger.debug(f"Cached patient baseline: {patient_id}")

    def get_cached_patient_baseline(self, patient_id: str) -> Optional[Dict[str, Any]]:
        """
        Get cached patient baseline data.

        Args:
            patient_id: Patient ID

        Returns:
            Baseline data or None if not cached
        """
        key = f"aimscribe:cache:patient_baseline:{patient_id}"
        value = self.client.get(key)
        if value:
            logger.debug(f"Cache hit: patient baseline {patient_id}")
            return json.loads(value)
        logger.debug(f"Cache miss: patient baseline {patient_id}")
        return None

    def invalidate_patient_baseline(self, patient_id: str):
        """Invalidate cached patient baseline."""
        key = f"aimscribe:cache:patient_baseline:{patient_id}"
        self.client.delete(key)

    def cache_session_context(
        self,
        session_id: str,
        context: Dict[str, Any],
        ttl: int = 1800
    ):
        """
        Cache session context (transcript + metadata).

        Args:
            session_id: Session ID
            context: Context dictionary with transcript, clip_count, etc.
            ttl: Time-to-live in seconds (default: 30 minutes)
        """
        key = f"aimscribe:cache:session_context:{session_id}"
        self.client.setex(key, ttl, json.dumps(context, default=str))
        logger.debug(f"Cached session context: {session_id}")

    def get_cached_session_context(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get cached session context.

        Args:
            session_id: Session ID

        Returns:
            Session context or None if not cached
        """
        key = f"aimscribe:cache:session_context:{session_id}"
        value = self.client.get(key)
        if value:
            logger.debug(f"Cache hit: session context {session_id}")
            return json.loads(value)
        logger.debug(f"Cache miss: session context {session_id}")
        return None

    def invalidate_session_context(self, session_id: str):
        """Invalidate cached session context (call after clip completion)."""
        key = f"aimscribe:cache:session_context:{session_id}"
        self.client.delete(key)

    def cache_previous_medications(
        self,
        patient_id: str,
        medications: Dict[str, Any],
        ttl: int = 3600
    ):
        """
        Cache patient's previous medications.

        Args:
            patient_id: Patient ID
            medications: Medications data from previous visit
            ttl: Time-to-live in seconds (default: 1 hour)
        """
        key = f"aimscribe:cache:prev_medications:{patient_id}"
        self.client.setex(key, ttl, json.dumps(medications, default=str))

    def get_cached_previous_medications(self, patient_id: str) -> Optional[Dict[str, Any]]:
        """Get cached previous medications."""
        key = f"aimscribe:cache:prev_medications:{patient_id}"
        value = self.client.get(key)
        if value:
            return json.loads(value)
        return None

    # ========== Utility ==========

    def healthcheck(self) -> bool:
        """Check if Redis is healthy."""
        try:
            return self.client.ping()
        except Exception:
            return False


# Convenience functions for common operations

def push_transcription_job(
    session_id: str,
    clip_number: int,
    object_key: str,
    patient_id: str,
    is_final: bool = False
) -> Dict[str, Any]:
    """
    Push a transcription job to the queue.
    
    Args:
        session_id: Session ID
        clip_number: Clip number
        object_key: MinIO object key
        patient_id: Patient ID
        is_final: Whether this is the final clip
        
    Returns:
        Job data that was pushed
    """
    job_data = {
        'job_id': f"{session_id}_{clip_number}",
        'job_type': 'transcription',
        'session_id': session_id,
        'clip_number': clip_number,
        'object_key': object_key,
        'patient_id': patient_id,
        'is_final': is_final
    }
    
    client = get_redis_client()
    client.push_job(TRANSCRIPTION_QUEUE, job_data)
    client.set_job_status(session_id, clip_number, 'queued')
    
    return job_data


# Global instance
_redis_client: Optional[RedisClient] = None


def get_redis_client() -> RedisClient:
    """Get or create the Redis client singleton."""
    global _redis_client
    
    if _redis_client is None:
        from config import settings
        
        _redis_client = RedisClient(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password
        )
    
    return _redis_client
