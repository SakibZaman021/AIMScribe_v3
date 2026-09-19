"""
AIMScribe AI Backend - Async Redis Client
Uses redis.asyncio (aioredis) for true async operations.
"""

import json
import logging
from typing import Optional, Dict, Any
from datetime import datetime

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# Queue names
TRANSCRIPTION_QUEUE = "aimscribe:transcription_queue"
NER_QUEUE = "aimscribe:ner_queue"
DEAD_LETTER_QUEUE = "aimscribe:dead_letter_queue"


class AsyncRedisClient:
    """
    Async Redis client for message queuing and caching.

    Features:
    - Async push/pop operations
    - Async caching with TTL
    - Non-blocking job status tracking
    """

    def __init__(
        self,
        host: str,
        port: int = 6379,
        db: int = 0,
        password: Optional[str] = None,
        ssl: bool = False
    ):
        self.host = host
        self.port = port
        self.db = db
        self.password = password
        self.ssl = ssl
        self._client: Optional[aioredis.Redis] = None

    async def initialize(self):
        """Initialize the async Redis connection."""
        self._client = aioredis.Redis(
            host=self.host,
            port=self.port,
            db=self.db,
            password=self.password,
            ssl=self.ssl,
            decode_responses=True,
            # The worker waits on BRPOP. Without these, a blocking pop that finds
            # nothing races the socket read deadline and surfaces as
            # "Timeout reading from redis:6379" on an otherwise healthy queue.
            socket_timeout=None,          # never time out a blocking read
            socket_connect_timeout=10,    # but do give up on a dead host
            socket_keepalive=True,
            health_check_interval=30,     # notice a silently dropped connection
        )
        # Test connection
        await self._client.ping()
        logger.info(f"Async Redis client connected: {self.host}:{self.port}")

    async def close(self):
        """Close the Redis connection."""
        if self._client:
            await self._client.close()
            logger.info("Async Redis client closed")

    async def healthcheck(self) -> bool:
        """Check if Redis is healthy."""
        try:
            await self._client.ping()
            return True
        except Exception:
            return False

    # ========== Queue Operations ==========

    async def push_job(self, queue: str, job_data: Dict[str, Any]) -> bool:
        """Push a job to a queue."""
        job_data['queued_at'] = datetime.now().isoformat()
        message = json.dumps(job_data)
        await self._client.lpush(queue, message)
        logger.debug(f"Pushed job to {queue}: {job_data.get('job_id', 'unknown')}")
        return True

    async def pop_job(self, queue: str, timeout: int = 0) -> Optional[Dict[str, Any]]:
        """Pop a job from a queue (blocking)."""
        result = await self._client.brpop(queue, timeout=timeout)
        if result:
            _, message = result
            job_data = json.loads(message)
            logger.debug(f"Popped job from {queue}: {job_data.get('job_id', 'unknown')}")
            return job_data
        return None

    async def pop_job_non_blocking(self, queue: str) -> Optional[Dict[str, Any]]:
        """Pop a job from a queue (non-blocking)."""
        message = await self._client.rpop(queue)
        if message:
            return json.loads(message)
        return None

    async def queue_length(self, queue: str) -> int:
        """Get the number of jobs in a queue."""
        return await self._client.llen(queue)

    async def move_to_dead_letter(self, job_data: Dict[str, Any], error: str):
        """Move a failed job to dead letter queue."""
        job_data['error'] = error
        job_data['failed_at'] = datetime.now().isoformat()
        await self.push_job(DEAD_LETTER_QUEUE, job_data)
        logger.warning(f"Moved job to dead letter queue: {job_data.get('job_id', 'unknown')}")

    # ========== Job Status Tracking ==========

    async def set_job_status(
        self,
        session_id: str,
        clip_number: int,
        status: str,
        details: Dict[str, Any] = None
    ):
        """Set the status of a processing job."""
        key = f"aimscribe:job_status:{session_id}:{clip_number}"
        value = {
            'status': status,
            'updated_at': datetime.now().isoformat(),
            **(details or {})
        }
        await self._client.setex(key, 3600 * 24, json.dumps(value))  # TTL: 24 hours

    async def get_job_status(self, session_id: str, clip_number: int) -> Optional[Dict]:
        """Get the status of a processing job."""
        key = f"aimscribe:job_status:{session_id}:{clip_number}"
        value = await self._client.get(key)
        if value:
            return json.loads(value)
        return None

    # ========== Caching for Performance ==========

    async def cache_patient_baseline(self, patient_id: str, baseline: Dict[str, Any], ttl: int = 3600):
        """Cache patient baseline data."""
        key = f"aimscribe:cache:patient_baseline:{patient_id}"
        await self._client.setex(key, ttl, json.dumps(baseline, default=str))
        logger.debug(f"Cached patient baseline: {patient_id}")

    async def get_cached_patient_baseline(self, patient_id: str) -> Optional[Dict[str, Any]]:
        """Get cached patient baseline data."""
        key = f"aimscribe:cache:patient_baseline:{patient_id}"
        value = await self._client.get(key)
        if value:
            logger.debug(f"Cache hit: patient baseline {patient_id}")
            return json.loads(value)
        logger.debug(f"Cache miss: patient baseline {patient_id}")
        return None

    async def invalidate_patient_baseline(self, patient_id: str):
        """Invalidate cached patient baseline."""
        key = f"aimscribe:cache:patient_baseline:{patient_id}"
        await self._client.delete(key)

    async def cache_session_context(
        self,
        session_id: str,
        context: Dict[str, Any],
        ttl: int = 1800
    ):
        """Cache session context (transcript + metadata)."""
        key = f"aimscribe:cache:session_context:{session_id}"
        await self._client.setex(key, ttl, json.dumps(context, default=str))
        logger.debug(f"Cached session context: {session_id}")

    async def get_cached_session_context(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get cached session context."""
        key = f"aimscribe:cache:session_context:{session_id}"
        value = await self._client.get(key)
        if value:
            logger.debug(f"Cache hit: session context {session_id}")
            return json.loads(value)
        logger.debug(f"Cache miss: session context {session_id}")
        return None

    async def invalidate_session_context(self, session_id: str):
        """Invalidate cached session context."""
        key = f"aimscribe:cache:session_context:{session_id}"
        await self._client.delete(key)

    async def cache_previous_medications(
        self,
        patient_id: str,
        medications: Dict[str, Any],
        ttl: int = 3600
    ):
        """Cache patient's previous medications."""
        key = f"aimscribe:cache:prev_medications:{patient_id}"
        await self._client.setex(key, ttl, json.dumps(medications, default=str))

    async def get_cached_previous_medications(self, patient_id: str) -> Optional[Dict[str, Any]]:
        """Get cached previous medications."""
        key = f"aimscribe:cache:prev_medications:{patient_id}"
        value = await self._client.get(key)
        if value:
            return json.loads(value)
        return None


# ============================================================================
# Convenience Functions
# ============================================================================

async def push_transcription_job_async(
    redis_client: AsyncRedisClient,
    session_id: str,
    clip_number: int,
    object_key: str,
    patient_id: str,
    is_final: bool = False
) -> Dict[str, Any]:
    """Push a transcription job to the queue."""
    job_data = {
        'job_id': f"{session_id}_{clip_number}",
        'job_type': 'transcription',
        'session_id': session_id,
        'clip_number': clip_number,
        'object_key': object_key,
        'patient_id': patient_id,
        'is_final': is_final
    }

    await redis_client.push_job(TRANSCRIPTION_QUEUE, job_data)
    await redis_client.set_job_status(session_id, clip_number, 'queued')

    return job_data
