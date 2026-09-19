"""
AIMScribe AI Backend - FastAPI Server (Async)
High-performance async API with true parallelism.
"""

import logging
import os
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from database.postgres_async import AsyncPostgreSQLDatabase
from storage.minio_client import get_minio_client, MinIOClient
from message_queue.redis_async import AsyncRedisClient, push_transcription_job_async

# Protocol 2: device identity, integrity chain, purge receipts, archive handshake.
import api_v2
from api_v2 import router as v2_router
from db_v2 import V2Repository
from integrity import ReceiptSigner

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format='%(asctime)s - API - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================================
# Pydantic Models (Request/Response)
# ============================================================================

class HealthScreeningInput(BaseModel):
    bp_systolic: str = None
    bp_diastolic: str = None
    diabetes_fasting: str = None
    diabetes_random: str = None
    height_cm: str = None
    weight_kg: str = None
    temperature: str = None
    pulse_rate: str = None


class SessionCreateRequest(BaseModel):
    patient_id: str
    session_id: str = None  # Client-provided session ID (PatientID_DoctorID_HospitalID_YYYYMMDD)
    patient_name: str = None
    age: str = None
    gender: str = None
    doctor_id: str = "DR_DEFAULT"
    hospital_id: str = "HOSP_DEFAULT"
    health_screening: HealthScreeningInput = None
    # Webhook URLs for CMED integration
    ner_webhook_url: str = None
    status_webhook_url: str = None


class SessionCreateResponse(BaseModel):
    session_id: str
    status: str
    created_at: str


class UploadRequestModel(BaseModel):
    session_id: str
    clip_number: int


class UploadCompleteRequest(BaseModel):
    session_id: str
    clip_number: int
    object_key: str
    is_final: bool = False


class StatusResponse(BaseModel):
    session_id: str
    status: str
    total_clips_transcribed: int
    has_transcript: bool
    has_ner: bool
    ner_version: int
    updated_at: str


class DoctorReviewRequest(BaseModel):
    session_id: str
    doctor_id: str
    field_name: str
    original_value: object = None
    edited_value: object = None


class PrescriptionRequest(BaseModel):
    session_id: str
    doctor_id: str
    prescription: dict


# ============================================================================
# Application Context (Async)
# ============================================================================

class AsyncAppContext:
    """Async application context with connection management."""

    def __init__(self):
        self.db: Optional[AsyncPostgreSQLDatabase] = None
        self.redis: Optional[AsyncRedisClient] = None
        self.minio: Optional[MinIOClient] = None

    async def initialize(self):
        """Initialize all async connections."""
        logger.info("Initializing async connections...")

        # Async PostgreSQL
        self.db = AsyncPostgreSQLDatabase(
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password,
            min_connections=settings.postgres_pool_min,
            max_connections=settings.postgres_pool_max,
            sslmode=settings.postgres_sslmode
        )
        await self.db.initialize()

        # Async Redis
        self.redis = AsyncRedisClient(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password,
            ssl=settings.redis_ssl
        )
        await self.redis.initialize()

        # MinIO (sync is fine - presigned URLs are fast)
        self.minio = get_minio_client()

        # Protocol 2 layer. Shares the pool above rather than opening a second
        # one, because Neon's connection budget is small.
        api_v2.ctx.repo = V2Repository(self.db.pool)
        api_v2.ctx.minio = self.minio
        api_v2.ctx.redis = self.redis
        api_v2.ctx.legacy_db = self.db
        api_v2.ctx.signer = ReceiptSigner.from_env()

        if api_v2.ctx.signer is None:
            logger.critical(
                "AIMS_RECEIPT_PRIVATE_KEY is not set. Audio will be received and "
                "archived, but no purge receipt can be issued, so agents will keep "
                "their local copies indefinitely."
            )

        logger.info("All async connections initialized")

    async def close(self):
        """Close all connections."""
        if self.db:
            await self.db.close()
        if self.redis:
            await self.redis.close()
        logger.info("All connections closed")


# Global context
ctx = AsyncAppContext()


# ============================================================================
# Application Lifespan
# ============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""
    # Startup
    await ctx.initialize()
    yield
    # Shutdown
    await ctx.close()


# ============================================================================
# FastAPI Application
# ============================================================================

app = FastAPI(
    title="AIMScribe AI Backend",
    description="Async API for Bengali Medical NER Extraction",
    version="6.0.0",
    lifespan=lifespan
)

# CORS middleware
#
# Was allow_origins=["*"] with allow_credentials=True, which lets any site on the
# internet call this API from a logged-in browser. Origins are now an exact
# allowlist from AIMS_ALLOWED_ORIGINS, and credentials are off - agents and the
# worker authenticate with headers, so cookies are never needed.
_allowed_origins = [o.strip() for o in os.getenv("AIMS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
if not _allowed_origins:
    logger.warning(
        "AIMS_ALLOWED_ORIGINS is not set; browser access is disabled. Set it to the "
        "exact CMED origin, e.g. https://cmed.example.com"
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-API-Key", "X-Device-Token"],
    max_age=600,
)

app.include_router(v2_router)


# ============================================================================
# Health Check
# ============================================================================

@app.get("/health")
async def health():
    """Health check endpoint."""
    db_healthy = await ctx.db.healthcheck() if ctx.db else False
    redis_healthy = await ctx.redis.healthcheck() if ctx.redis else False

    return {
        "status": "healthy" if (db_healthy and redis_healthy) else "degraded",
        "database": "connected" if db_healthy else "disconnected",
        "redis": "connected" if redis_healthy else "disconnected",
        "minio": "connected",
        "version": "6.0.0",
        "mode": "FastAPI Async"
    }


async def _require_admin_key(x_admin_key: Optional[str] = Header(None)) -> None:
    """Admin gate for diagnostics. Constant-time to avoid leaking the key by timing."""
    import hmac as _hmac
    expected = os.getenv("AIMS_ADMIN_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="admin key is not configured")
    if not x_admin_key or not _hmac.compare_digest(x_admin_key, expected):
        raise HTTPException(status_code=401, detail="invalid admin key")


@app.get("/api/v1/diagnostic/azure-ner")
async def diagnostic_azure_ner(_: None = Depends(_require_admin_key)):
    """
    Test Azure OpenAI NER connectivity.

    Now admin-only. Unauthenticated, this returned the Azure endpoint, deployment
    name, API version and whether a key was configured - a free reconnaissance
    endpoint for anyone who found the service.
    """
    import httpx
    from config import settings

    endpoint = settings.azure_ner_endpoint
    deployment = settings.azure_ner_deployment
    api_version = settings.azure_api_version
    api_key = settings.azure_ner_api_key

    # Build the full URL
    url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"

    # Simple test payload
    payload = {
        "messages": [{"role": "user", "content": "Say hello"}],
        "max_completion_tokens": 10
    }

    headers = {
        "Content-Type": "application/json",
        "api-key": api_key
    }

    result = {
        "endpoint": endpoint,
        "deployment": deployment,
        "api_version": api_version,
        "url_tested": url,
        "api_key_present": bool(api_key and len(api_key) > 10),
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            result["status_code"] = response.status_code
            result["success"] = response.status_code == 200

            if response.status_code == 200:
                data = response.json()
                result["model"] = data.get("model", "unknown")
                result["message"] = "Azure NER connection successful"
            else:
                result["error"] = response.text[:500]
                result["message"] = "Azure NER connection failed"

    except httpx.ConnectError as e:
        result["success"] = False
        result["error_type"] = "ConnectError"
        result["error"] = str(e)
        result["message"] = "Failed to connect to Azure endpoint"

    except httpx.TimeoutException as e:
        result["success"] = False
        result["error_type"] = "TimeoutError"
        result["error"] = str(e)
        result["message"] = "Connection timed out"

    except Exception as e:
        result["success"] = False
        result["error_type"] = type(e).__name__
        result["error"] = str(e)
        result["message"] = f"Unexpected error: {type(e).__name__}"

    return result


# ============================================================================
# Session Management
# ============================================================================

@app.post("/api/v1/session/create", response_model=SessionCreateResponse)
async def create_session(request: SessionCreateRequest):
    """Create a new recording session with patient demographics and health screening."""
    try:
        # Convert health_screening to dict if provided
        health_screening_dict = None
        if request.health_screening:
            health_screening_dict = request.health_screening.model_dump(exclude_none=True)

        # Create session with health screening and webhook URLs (async)
        # Use client-provided session_id if available
        session_id = await ctx.db.create_session(
            request.patient_id,
            request.doctor_id,
            request.hospital_id,
            session_id=request.session_id,  # Client-provided session ID
            health_screening=health_screening_dict,
            ner_webhook_url=request.ner_webhook_url,
            status_webhook_url=request.status_webhook_url
        )

        # Upsert patient with demographics from request
        patient_data = {
            'patient_id': request.patient_id,
            'name': request.patient_name or 'Unknown Patient',
            'age': request.age or 'N/A',
            'gender': request.gender or 'N/A'
        }
        await ctx.db.upsert_patient(patient_data)

        return SessionCreateResponse(
            session_id=session_id,
            status="active",
            created_at=datetime.now().isoformat()
        )

    except Exception as e:
        logger.error(f"Create session failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Audio Upload (Presigned URLs)
# ============================================================================

@app.post("/api/v1/upload/request")
async def request_upload_url(request: UploadRequestModel):
    """Get a presigned URL for uploading an audio clip."""
    try:
        # Generate object key
        object_key = MinIOClient.generate_object_key(
            request.session_id,
            request.clip_number
        )

        # Generate presigned URL (sync - fast operation)
        upload_url = ctx.minio.get_presigned_upload_url(object_key, expires=300)

        # Create pending record (async)
        await ctx.db.save_clip_record(
            request.session_id,
            request.clip_number,
            object_key
        )

        return {
            "upload_url": upload_url,
            "object_key": object_key,
            "expires_in": 300
        }

    except Exception as e:
        logger.error(f"Presigned URL generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/upload/complete")
async def complete_upload(request: UploadCompleteRequest):
    """Notify that upload is complete. Pushes job to processing queue."""
    try:
        # Verify session exists (async)
        session = await ctx.db.get_session(request.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Push to Redis queue (async)
        job_data = await push_transcription_job_async(
            ctx.redis,
            session_id=request.session_id,
            clip_number=request.clip_number,
            object_key=request.object_key,
            patient_id=session['patient_id'],
            is_final=request.is_final
        )

        # Get queue length (async)
        queue_length = await ctx.redis.queue_length("aimscribe:transcription_queue")

        return {
            "status": "queued",
            "job_id": job_data['job_id'],
            "queue_position": queue_length
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Job queuing failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Results & Monitoring
# ============================================================================

@app.get("/api/v1/session/{session_id}/status", response_model=StatusResponse)
async def get_session_status(session_id: str):
    """Get processing status for a session."""
    try:
        # All queries run concurrently with asyncio.gather
        import asyncio

        session, full_transcript, ner_result, clip_count = await asyncio.gather(
            ctx.db.get_session(session_id),
            ctx.db.get_full_transcript(session_id),
            ctx.db.get_latest_ner(session_id),
            ctx.db.get_clip_count(session_id)
        )

        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        return StatusResponse(
            session_id=session_id,
            status=session['status'],
            total_clips_transcribed=clip_count,
            has_transcript=bool(full_transcript),
            has_ner=bool(ner_result),
            ner_version=ner_result['version'] if ner_result else 0,
            updated_at=datetime.now().isoformat()
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Status check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/transcript/{session_id}")
async def get_transcript(session_id: str):
    """Get full cumulative transcript."""
    try:
        transcript = await ctx.db.get_full_transcript(session_id)
        return {
            "session_id": session_id,
            "transcript": transcript or ""
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/ner/{session_id}")
async def get_ner_result(session_id: str):
    """Get latest NER result with individual fields."""
    try:
        result = await ctx.db.get_latest_ner(session_id)
        if not result:
            return {"version": 0, "fields": None}

        fields = {
            "patient_name": result.get('patient_name'),
            "age": result.get('age'),
            "gender": result.get('gender'),
            "chief_complaints": result.get('chief_complaints'),
            "drug_history": result.get('drug_history'),
            "on_examination": result.get('on_examination'),
            "systemic_examination": result.get('systemic_examination'),
            "additional_notes": result.get('additional_notes'),
            "investigations": result.get('investigations'),
            "diagnosis": result.get('diagnosis'),
            "medications": result.get('medications'),
            "advice": result.get('advice'),
            "follow_up": result.get('follow_up'),
            "health_screening": result.get('health_screening'),
        }

        return {
            "version": result['version'],
            "is_final": result['is_final'],
            "created_at": result['created_at'],
            "fields": fields,
            "full_ner_json": result.get('full_ner_json')
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Doctor Reviews
# ============================================================================

@app.post("/api/v1/doctor-review")
async def save_doctor_review(request: DoctorReviewRequest):
    """Save a doctor's edit to a NER field."""
    try:
        review_id = await ctx.db.save_doctor_review(
            session_id=request.session_id,
            doctor_id=request.doctor_id,
            field_name=request.field_name,
            original_value=request.original_value,
            edited_value=request.edited_value
        )
        return {"review_id": review_id, "status": "saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/doctor-review/{session_id}")
async def get_doctor_reviews(session_id: str):
    """Get all doctor reviews for a session."""
    try:
        reviews = await ctx.db.get_doctor_reviews(session_id)
        return {"session_id": session_id, "reviews": reviews}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Prescriptions
# ============================================================================

@app.post("/api/v1/prescription")
async def save_prescription(request: PrescriptionRequest):
    """Save final prescription data (doctor presses Print)."""
    try:
        # Get patient_id from session
        session = await ctx.db.get_session(request.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        prescription_id = await ctx.db.save_prescription(
            session_id=request.session_id,
            patient_id=session['patient_id'],
            doctor_id=request.doctor_id,
            prescription=request.prescription
        )
        return {"prescription_id": prescription_id, "status": "saved"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/prescription/{session_id}")
async def get_prescription(session_id: str):
    """Get latest prescription for a session."""
    try:
        prescription = await ctx.db.get_prescription(session_id)
        if not prescription:
            return {"session_id": session_id, "prescription": None}
        return {"session_id": session_id, "prescription": prescription}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Entry Point
# ============================================================================

def main():
    """Run the FastAPI server."""
    import uvicorn

    logger.info(f"Starting AIMScribe FastAPI on port {settings.server_port}")
    uvicorn.run(
        "main_fastapi:app",
        host="0.0.0.0",
        port=settings.server_port,
        reload=settings.debug,
        workers=4  # Multiple workers for production
    )


if __name__ == "__main__":
    main()
