"""
AIMScribe AI Backend - FastAPI Server (Async)
High-performance async API with true parallelism.
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from config import settings
from database.postgres_async import AsyncPostgreSQLDatabase
from storage.minio_client import get_minio_client, MinIOClient
from message_queue.redis_async import AsyncRedisClient

# Protocol 2: device identity, integrity chain, purge receipts, archive handshake.
import api_v2
import schema_check
from api_v2 import router as v2_router
from clinical import router as clinical_router
from dashboard import router as dashboard_router
import cmed_auth
import fhir
import security
from grants import GrantIssuer
from clinical_store import ClinicalStore
from db_v2 import V2Repository
from integrity import ReceiptSigner

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    # Every line names its request (security.REQUEST_ID), so one failure can be
    # followed from the gateway through the API to the archive worker.
    format='%(asctime)s - API - %(levelname)s - [%(request_id)s] %(message)s'
)
logger = logging.getLogger(__name__)


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
        # SRS 3.2 §5: this server issues recording grants. Without the key
        # recorders refuse to record, which is the safe direction.
        api_v2.ctx.grants = GrantIssuer.from_env()

        # SRS 3.2 §8.7: patient information lives in its own database, with its
        # own credentials, so a role that can search recordings cannot read it.
        clinical_url = os.getenv("AIMS_CLINICAL_DATABASE_URL", "").strip()
        self.clinical_pool = None
        if clinical_url:
            import asyncpg
            from database.postgres_async import pooler_options
            self.clinical_pool = await asyncpg.create_pool(
                clinical_url, min_size=1, max_size=5, command_timeout=30,
                **pooler_options())
            api_v2.ctx.clinical = ClinicalStore(self.clinical_pool, separate=True)
        else:
            logger.critical(
                "AIMS_CLINICAL_DATABASE_URL is not set - CMED's clinical records are "
                "being kept in the recordings database. Set it before real patient "
                "data arrives (SRS-DBA-20).")
            api_v2.ctx.clinical = ClinicalStore(self.db.pool, separate=False)

        # SRS-STO-01, SRS-ARC-12: the copy bucket, with its own credentials.
        # That token is read-and-write to the copy bucket only, and the token
        # above - which deletes pieces - has no access to it at all, so no one
        # credential can both make a copy and destroy one.
        api_v2.ctx.copy = self._copy_bucket()
        # The JSON copies, if they are kept somewhere warmer than the audio -
        # cold storage bills a minimum object size and answers in hours, and a
        # visit's JSON is two kilobytes that someone may want to read today.
        api_v2.ctx.copy_json = self._copy_bucket(prefix="AIMS_JSON") \
            if os.getenv("AIMS_JSON_BUCKET", "").strip() else None

        if api_v2.ctx.signer is None:
            logger.critical(
                "AIMS_RECEIPT_PRIVATE_KEY is not set. Audio will be received and "
                "archived, but no purge receipt can be issued, so agents will keep "
                "their local copies indefinitely."
            )

        logger.info("All async connections initialized")

    @staticmethod
    def _copy_bucket(prefix: str = "AIMS_COPY") -> Optional[MinIOClient]:
        """
        A store for the copies. `AIMS_COPY_*` is the audio's, normally cold
        storage at another provider; `AIMS_JSON_*` is the warm one for the
        clinical JSON, and is optional.

        Unset means the copy step is not running yet: the archive worker has
        nothing to copy to, and /archive/complete keeps deleting the pieces as
        it did before, saying so each time.
        """
        bucket = os.getenv(f"{prefix}_BUCKET", "").strip()
        if bucket and bucket == settings.minio_bucket:
            logger.critical(
                "%s_BUCKET is the same bucket as the pieces (%s). The copy must be "
                "a separate bucket, locked against deletion (SRS-ARC-12). No copies "
                "will be made until this is fixed.", prefix, bucket)
            return None
        if not bucket:
            if prefix == "AIMS_COPY":
                logger.warning(
                    "AIMS_COPY_BUCKET is not set - no lossless cloud copy is being "
                    "kept (SRS-ARC-08). Pieces are deleted as soon as a recording "
                    "is archived.")
            return None
        try:
            # Each store is reached with its own credentials. The segment
            # bucket's token must not open either of these, and neither of
            # these may delete from the segment bucket (SRS-ARC-12, SRS-STO-02).
            return MinIOClient(
                endpoint=os.getenv(f"{prefix}_ENDPOINT", settings.minio_endpoint).strip(),
                access_key=os.getenv(f"{prefix}_ACCESS_KEY", "").strip(),
                secret_key=os.getenv(f"{prefix}_SECRET_KEY", "").strip(),
                bucket=bucket,
                secure=os.getenv(f"{prefix}_SECURE", "true").lower() != "false",
                region=os.getenv(f"{prefix}_REGION", settings.minio_region).strip(),
            )
        except Exception as exc:
            # Never fatal: recordings must keep arriving. The copies simply wait.
            logger.critical("Copy bucket %s could not be opened: %s", bucket, exc)
            return None

    async def close(self):
        """Close all connections."""
        if getattr(self, "clinical_pool", None) is not None:
            await self.clinical_pool.close()
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
    # Every migration this code needs, present? Said loudly at start, shown in
    # /health, and fatal only if AIMS_SCHEMA_STRICT asks for it.
    pools = _schema_pools()
    if pools[0] is not None:
        await schema_check.at_startup(*pools, repo=api_v2.ctx.repo)
    yield
    # Shutdown
    await ctx.close()


# ============================================================================
# FastAPI Application
# ============================================================================

# The interactive API pages (/docs, /redoc, /openapi.json) listed every route
# to anyone on the internet. Off unless a developer machine asks for them.
_DOCS = os.getenv("AIMS_ENABLE_API_DOCS", "").strip().lower() in ("1", "true", "yes")

app = FastAPI(
    title="AIMScribe v3",
    version="3.5.0",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS else None,
    redoc_url="/redoc" if _DOCS else None,
    openapi_url="/openapi.json" if _DOCS else None,
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
    allow_headers=["Content-Type", "X-Device-Token"],
    max_age=600,
)

# Every request: a per-address rate limit, and errors that never carry
# internals (stack traces, SQL, file paths) back to the caller.
security.install(app)

app.include_router(v2_router)
# CMED's identity: per-hospital clients, signed assertions, short-lived tokens
# (SMART Backend Services), and the FHIR R4 form of API 2 and API 3b.
app.include_router(cmed_auth.router)
app.include_router(fhir.router)
app.include_router(fhir.well_known)
# Google sign-in for the dashboard and the database viewer (portal.py).
import portal  # noqa: E402
app.include_router(portal.router)
# Channel B: CMED's server sends API 2 and prescriptions here (SRS 3.2 §6.2).
app.include_router(clinical_router)
# The operational dashboard (SRS 3.3 §8.9). Reads the recordings database
# only, behind the administrator key.
app.include_router(dashboard_router)


# ============================================================================
# The front page
#
# Opening the server in a browser used to give `{"detail":"Not Found"}`, which
# says nothing at all - least of all whether this is the new server or the one
# that has been running the clinics. This page answers that first.
# ============================================================================

SYSTEM_NAME = "AIMScribe v3 - AIMS LAB server"
SYSTEM_VERSION = "3.5.0"
SRS_VERSION = "3.3"

_FRONT_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIMScribe v3</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.6 system-ui, "Segoe UI", sans-serif; margin: 0;
         padding: 3rem 1.5rem; display: flex; justify-content: center; }}
  main {{ max-width: 44rem; width: 100%; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 .25rem; }}
  .sub {{ opacity: .7; margin: 0 0 2rem; }}
  .tag {{ display: inline-block; padding: .15rem .6rem; border-radius: 999px;
          background: #1f7a3f; color: #fff; font-size: .8rem;
          vertical-align: middle; margin-left: .5rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0 0 2rem; }}
  td {{ padding: .5rem .75rem; border-bottom: 1px solid rgba(128,128,128,.25);
        vertical-align: top; }}
  td:first-child {{ white-space: nowrap; opacity: .7; width: 12rem; }}
  code {{ font-size: .9em; }}
  footer {{ opacity: .65; font-size: .9rem; }}
</style>
</head>
<body><main>
  <h1>AIMScribe v3 <span class="tag">{version}</span></h1>
  <p class="sub">The AIMS LAB server - SRS {srs}. This is <strong>not</strong>
     the version&nbsp;1 backend on Render.</p>
  <table>
    <tr><td>Health</td><td><a href="/health">/health</a></td></tr>
    <tr><td>Dashboard</td><td><a href="/api/v2/dashboard">/api/v2/dashboard</a>
        - asks for the administrator key</td></tr>
    <tr><td>Recorders talk to</td><td><code>/api/v2/…</code></td></tr>
    <tr><td>CMED talks to</td><td><code>/fhir/r4</code> (FHIR R4, signed per-hospital
        tokens from <code>/api/v2/oauth/token</code>)</td></tr>
  </table>
  <footer>Recordings are held at UIU. Nothing on this page reads a
  consultation.</footer>
</main></body>
</html>
"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def front_page():
    return _FRONT_PAGE.format(version=SYSTEM_VERSION, srs=SRS_VERSION)


# ============================================================================
# Health Check
# ============================================================================

# How long one answer about object storage is reused. Asking the store costs a
# round trip to the provider; /health is polled by Docker, the monitor and
# uptime checks, and none of them needs an answer fresher than this.
STORAGE_CHECK_SECONDS = 30
STORAGE_CHECK_TIMEOUT = 4.0
_storage_cache: dict = {"at": 0.0, "answer": None}
_storage_lock = asyncio.Lock()


async def _bucket_state(client: Optional[MinIOClient]) -> str:
    """
    Whether one bucket answers with the credentials this server holds.

    This used to be the constant "connected", so a server with the wrong R2
    keys reported healthy while every upload failed. A HEAD on the bucket is
    the cheapest request that proves the endpoint, the keys and the bucket
    name together.
    """
    if client is None:
        return "not configured"
    try:
        exists = await asyncio.wait_for(
            asyncio.to_thread(client.client.bucket_exists, client.bucket),
            timeout=STORAGE_CHECK_TIMEOUT)
    except asyncio.TimeoutError:
        return "unreachable"
    except Exception as exc:
        logger.warning("Storage check for %s failed: %s", client.bucket, exc)
        return "refused"
    return "connected" if exists else "bucket missing"


async def _storage_states() -> dict:
    now = time.monotonic()
    cached = _storage_cache["answer"]
    if cached is not None and now - _storage_cache["at"] < STORAGE_CHECK_SECONDS:
        return cached
    async with _storage_lock:
        if _storage_cache["answer"] is not None and                 time.monotonic() - _storage_cache["at"] < STORAGE_CHECK_SECONDS:
            return _storage_cache["answer"]
        segments, copies = await asyncio.gather(
            _bucket_state(ctx.minio), _bucket_state(api_v2.ctx.copy))
        answer = {"minio": segments, "copy_bucket": copies}
        _storage_cache.update(at=time.monotonic(), answer=answer)
        return answer


def _schema_pools():
    """The recordings pool, and the clinical one when it is a separate database."""
    recordings = getattr(getattr(ctx, "db", None), "pool", None)
    clinical = getattr(getattr(api_v2.ctx, "clinical", None), "_pool", None)
    return recordings, clinical


async def _schema_state() -> str:
    recordings, clinical = _schema_pools()
    if recordings is None:
        return "not checked"
    await schema_check.STATE.check(recordings, clinical)
    return schema_check.STATE.summary()


async def _details() -> dict:
    db_healthy = await ctx.db.healthcheck() if ctx.db else False
    redis_healthy = await ctx.redis.healthcheck() if ctx.redis else False
    storage = await _storage_states()
    schema = await _schema_state() if db_healthy else "not checked"
    # The segment bucket is where every recording lands, so it counts. The
    # copy bucket counts only if it is configured: unset is a choice, but set
    # and failing means copies are silently not being made.
    storage_healthy = storage["minio"] == "connected" and         storage["copy_bucket"] in ("connected", "not configured")

    return {
        "status": "healthy" if (db_healthy and redis_healthy and storage_healthy
                                and schema == "ok") else "degraded",
        "database": "connected" if db_healthy else "disconnected",
        # "ok", or the migration files still to apply (e.g. "missing
        # 015_v3_consultation_parts.sql"). Read by the monitor.
        "schema": schema,
        "redis": "connected" if redis_healthy else "disconnected",
        "minio": storage["minio"],
        "copy_bucket": storage["copy_bucket"],
        # Which AIMScribe this is. A machine can be running the live v1
        # server and this one at the same time, and they answer on ports
        # that look alike; anything talking to the wrong one is a long
        # afternoon. The name is the first thing in the answer for that
        # reason.
        "system": SYSTEM_NAME,
        "srs": SRS_VERSION,
        "version": SYSTEM_VERSION,
        "mode": "FastAPI Async",
    }


def _may_see_details(request: Request) -> bool:
    """
    The full answer (versions, missing migrations, storage states) is for the
    operator: the monitor and Docker, which call the API directly on the
    internal network, or anyone with the admin key. Through the public
    gateway a caller learns only whether the server is up.
    """
    import hmac as _hmac
    supplied = request.headers.get("x-admin-key") or ""
    expected = os.getenv("AIMS_ADMIN_KEY", "")
    if expected and supplied and _hmac.compare_digest(supplied, expected):
        return True
    return "x-forwarded-for" not in request.headers


@app.get("/health")
async def health(request: Request):
    """Health: always 200 while the process runs; the body says how well."""
    answer = await _details()
    if _may_see_details(request):
        return answer
    return {"status": answer["status"], "system": SYSTEM_NAME}


@app.get("/health/live")
async def health_live():
    """The process is up and answering. For Docker's restart decision."""
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready(request: Request):
    """
    503 unless this server can do its work: database reachable, every
    migration applied, the segment store answering. For load balancers and
    the monitor - a server that is up but cannot store a recording is not
    ready, and saying 200 there hid exactly that.
    """
    answer = await _details()
    ready = (answer["database"] == "connected" and answer["schema"] == "ok"
             and answer["minio"] == "connected")
    body = answer if _may_see_details(request) else {"status": answer["status"]}
    return JSONResponse(status_code=200 if ready else 503,
                        content={**body, "ready": ready})


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
        workers=settings.server_workers,
        # A recorder keeps one connection and uses it every thirty seconds.
        # uvicorn closes an idle connection after five, and when that lands on
        # the moment the recorder reuses it the piece fails with "server
        # disconnected" - the recorder retries and no audio is lost, but the
        # retry is noise that looks like a fault. Longer than the gap between
        # pieces, so the connection is still there when it is next needed.
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    main()
