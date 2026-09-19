"""
Local control API for the AIMScribe agent.

Bound to loopback and driven by CMED running on the same PC. The WebSocket in
`websocket_server.py` is the primary channel; these HTTP routes exist for health
checks, status polling, and administrative actions.

Security changes from v1, all of which were exploitable from any web page the
doctor visited:

* `allow_origins=["*"]` with `allow_credentials=True` is gone. Origins are an
  exact allowlist and credentials are not used.
* `verify_api_key` was a stub that always returned True with the real check
  commented out. It now enforces, in constant time.
* The unauthenticated legacy aliases `/trigger`, `/stop`, `/status` and
  `/force-reset` are removed. They bypassed every control above them.
* A `Host` header allowlist blocks DNS rebinding.
* Errors no longer return `str(exc)` to the caller.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import crypto
from core.session_controller import SessionController, SessionError
from core.spool import Spool
from core.uploader import UploadManager
from api.websocket_server import GrantGuard, WebSocketManager

logger = logging.getLogger(__name__)


# ============================================================
# Request models
# ============================================================

class PauseRequest(BaseModel):
    reason: str = Field(..., max_length=64)
    reason_detail: str = Field("", max_length=500)
    authorised_by: str = Field("", max_length=120)
    expected_seconds: int = Field(0, ge=0, le=86400)


class ForceResetRequest(BaseModel):
    actor: str = Field(..., max_length=120)
    reason: str = Field("", max_length=500)


# ============================================================
# Runtime
# ============================================================

class Runtime:
    """
    Owns every long-lived object the agent needs.

    Constructed by main.py, started and stopped by the FastAPI lifespan so that
    everything async lives on one event loop.
    """

    def __init__(self, cfg, *, log_salt: bytes):
        self.cfg = cfg
        self.log_salt = log_salt
        self.device_key = crypto.DeviceKey.load_or_create(
            cfg.security.device_key_path,
            allow_plaintext=cfg.security.allow_plaintext_keystore,
        )
        self.spool = Spool.from_config(cfg)
        self.ws = WebSocketManager(cfg, grant_guard=GrantGuard())
        self.uploader: Optional[UploadManager] = None
        self.controller: Optional[SessionController] = None
        self.identity = None
        self.problems = cfg.validate()
        self.warnings = cfg.production_warnings()
        self._loop = None

    @property
    def loop(self):
        """
        The agent's event loop, for callers on another thread.

        The tray runs on its own thread and must not touch the controller
        directly; it schedules onto this instead.
        """
        return self._loop

    # ---- lifecycle ----

    async def startup(self) -> None:
        import asyncio

        self._loop = asyncio.get_running_loop()

        receipt_key = None
        try:
            receipt_key = crypto.load_public_key(self.cfg.security.receipt_public_key_path)
        except Exception as exc:
            logger.critical("Purge-receipt key unavailable (%s); local audio will never "
                            "be deleted automatically", exc)

        if self.cfg.security.require_grant:
            try:
                self.ws.set_grant_key(
                    crypto.load_public_key(self.cfg.security.grant_public_key_path))
            except Exception as exc:
                logger.critical("Grant key unavailable (%s); recordings cannot start", exc)

        # Enrollment runs before anything else that needs an identity. If it
        # cannot complete, the agent still starts and shows its tray icon - it
        # simply refuses to record, which is far easier to diagnose than a silent
        # failure or, worse, sessions filed under a guessed hospital.
        from core.enrollment import ensure_enrolled
        from core.uploader import build_ssl_context

        try:
            self.identity = await ensure_enrolled(
                self.cfg, self.device_key, ssl_context=build_ssl_context(self.cfg))
        except Exception as exc:
            logger.error("Enrollment check failed: %s", exc)

        if self.identity is None:
            self.problems.append(
                "This device is not enrolled. An administrator must install it with "
                "-EnrollmentToken, or place enrollment.token in the state folder.")

        from core.enrollment import load_device_token

        self.uploader = UploadManager(
            self.cfg,
            device_key=self.device_key,
            spool=self.spool,
            receipt_public_key=receipt_key,
            on_event=self._on_event,
        )
        self.uploader.set_device_token(load_device_token(self.cfg))
        await self.uploader.start()

        self.controller = SessionController(
            self.cfg,
            device_key=self.device_key,
            spool=self.spool,
            uploader=self.uploader,
            on_event=self._on_event,
            log_salt=self.log_salt,
        )
        if self.identity is not None:
            self.controller.device_id = self.identity.device_id
            self.controller.hospital_id = self.identity.hospital_id
            self.controller.doctor_id = self.identity.doctor_id

        await self.controller.start()
        self.ws.set_controller(self.controller)
        self.ws.set_register_source(self.uploader, self.controller.hospital_id)

        logger.info("Agent ready - %s at %s, device %s (%s), spool holds %.1f h",
                    self.identity.doctor_id if self.identity else "NO DOCTOR",
                    self.identity.hospital_id if self.identity else "NO HOSPITAL",
                    self.identity.device_id if self.identity else "UNENROLLED",
                    self.device_key.fingerprint(), self.cfg.spool_seconds() / 3600)

        if self.identity is not None and not self.identity.doctor_id:
            logger.warning(
                "No doctor is assigned to this PC. Recording still works - CMED "
                "names the doctor for each consultation - but a consultation "
                "started without one will be refused.")
        for problem in self.problems:
            logger.critical("CONFIGURATION PROBLEM: %s", problem)
        for warning in self.warnings:
            logger.warning("PRODUCTION WARNING: %s", warning)

    async def shutdown(self) -> None:
        if self.controller:
            await self.controller.close()
        if self.uploader:
            await self.uploader.stop()

    # ---- events ----

    def _on_event(self, event: str, data: Dict[str, Any]) -> None:
        """
        Fan controller and uploader events out to CMED.

        Called from both the event loop and worker threads, so the broadcast is
        always scheduled onto the loop rather than awaited directly.
        """
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(
                lambda: self._loop.create_task(self.ws.send_event(event, data)))
        except RuntimeError:
            pass

    # ---- status ----

    def health(self) -> Dict[str, Any]:
        return {
            "status": "degraded" if self.problems else "healthy",
            "service": "aimscribe-agent",
            "version": self.cfg.app_version,
            "protocol_version": self.cfg.protocol_version,
            "device": self.device_key.fingerprint(),
            "problems": self.problems,
            "timestamp": crypto.iso_utc(datetime.now(timezone.utc)),
        }


# ============================================================
# Application
# ============================================================

def create_app(runtime: Runtime) -> FastAPI:
    cfg = runtime.cfg

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.startup()
        try:
            yield
        finally:
            await runtime.shutdown()

    app = FastAPI(
        title="AIMScribe Agent",
        version=cfg.app_version,
        docs_url="/docs" if cfg.security.enable_docs else None,
        redoc_url="/redoc" if cfg.security.enable_docs else None,
        openapi_url="/openapi.json" if cfg.security.enable_docs else None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime

    # Exact origins only, and no credentials: the API key is a header, so cookies
    # are never needed and enabling them would widen the attack surface for free.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(cfg.security.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key"],
        max_age=600,
    )

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Host allowlist on everything; Origin allowlist whenever one is present."""
        host = request.headers.get("host")
        if not cfg.security.host_allowed(host):
            logger.warning("Rejected request with Host %r (possible DNS rebinding)", host)
            return _error_response(403, "FORBIDDEN_HOST", "Host not allowed")

        origin = request.headers.get("origin")
        if origin is not None and not cfg.security.origin_allowed(origin):
            logger.warning("Rejected request from origin %r", origin)
            return _error_response(403, "FORBIDDEN_ORIGIN", "Origin not allowed")

        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # ---- auth ----

    def require_api_key(x_api_key: Optional[str] = Header(None)) -> None:
        expected = cfg.security.local_api_key
        if not expected:
            raise HTTPException(status_code=503, detail="Agent is not configured")
        if not x_api_key or not secrets.compare_digest(x_api_key, expected):
            raise HTTPException(status_code=401, detail="Invalid API key")

    def controller() -> SessionController:
        if runtime.controller is None:
            raise HTTPException(status_code=503, detail="Agent is still starting")
        return runtime.controller

    # ---- error handling ----

    @app.exception_handler(HTTPException)
    async def on_http_error(request: Request, exc: HTTPException):
        return _error_response(exc.status_code, "HTTP_ERROR", str(exc.detail))

    @app.exception_handler(SessionError)
    async def on_session_error(request: Request, exc: SessionError):
        return _error_response(409, "SESSION_REFUSED", str(exc))

    @app.exception_handler(Exception)
    async def on_unhandled(request: Request, exc: Exception):
        # Detail stays in the log. Returning str(exc) to the caller, as v1 did,
        # leaks file paths and internal state to anything that can reach the port.
        reference = secrets.token_hex(6)
        logger.error("Unhandled error [%s] on %s: %s",
                     reference, request.url.path, exc, exc_info=True)
        return _error_response(500, "INTERNAL_ERROR",
                               f"Internal error (reference {reference})")

    # ---- routes ----

    @app.get("/health")
    async def health():
        """Unauthenticated liveness probe. Reveals no session or patient data."""
        return runtime.health()

    @app.get("/api/v1/session/status")
    async def session_status(_: None = Depends(require_api_key)):
        return {"success": True, "data": controller().status()}

    @app.get("/api/v1/doctors")
    async def doctors(_: None = Depends(require_api_key)):
        """
        Who may record on this PC today.

        A consulting room is shared, so the page asks the agent rather than
        assuming the machine's own doctor. The list is fetched here, not by the
        browser, because the device token that authorises it must stay on this
        machine - and because the agent knows which hospital it belongs to,
        while the browser only knows what it was told.
        """
        assigned = runtime.identity.doctor_id if runtime.identity else ""
        hospital = runtime.identity.hospital_id if runtime.identity else ""
        register = await runtime.uploader.fetch_doctors(hospital) if runtime.uploader else None
        return {"success": True, "data": {
            "hospital_id": hospital or None,
            # Deliberately absent: the machine has no doctor. Advertising one
            # is what made the page pre-select it and file an afternoon
            # consultation under the morning doctor.
            "doctors": register or [],
        }}

    @app.post("/api/v1/session/stop")
    async def session_stop(_: None = Depends(require_api_key)):
        return {"success": True, "data": await controller().stop_session()}

    @app.post("/api/v1/session/pause")
    async def session_pause(body: PauseRequest, _: None = Depends(require_api_key)):
        return {"success": True, "data": await controller().pause_session(
            reason=body.reason,
            reason_detail=body.reason_detail,
            authorised_by=body.authorised_by,
            expected_seconds=body.expected_seconds,
        )}

    @app.post("/api/v1/session/resume")
    async def session_resume(_: None = Depends(require_api_key)):
        return {"success": True, "data": await controller().resume_session()}

    @app.post("/api/v1/session/force-reset")
    async def session_force_reset(body: ForceResetRequest, _: None = Depends(require_api_key)):
        """
        Clear a stuck state. Audio already sealed is preserved and keeps uploading.

        Requires a named actor and is recorded as an integrity event, because a
        reset is exactly the kind of action that needs to be attributable.
        """
        return {"success": True, "data": await controller().force_reset(
            actor=body.actor, reason=body.reason)}

    @app.get("/api/v1/diagnostics")
    async def diagnostics(_: None = Depends(require_api_key)):
        """Everything support needs, with no patient identifiers in the response."""
        from core.recorder import AudioRecorder
        try:
            devices = [d.name for d in AudioRecorder.list_input_devices()]
        except Exception:
            devices = []
        return {"success": True, "data": {
            "version": cfg.app_version,
            "device": runtime.device_key.fingerprint(),
            "problems": runtime.problems,
            "warnings": runtime.warnings,
            "input_devices": devices,
            "spool": {
                "bytes": runtime.spool.total_bytes(),
                "pressure": runtime.spool.pressure(),
                "capacity_hours": round(cfg.spool_seconds() / 3600, 1),
            },
            "upload": runtime.uploader.status() if runtime.uploader else {},
            "connected_clients": runtime.ws.client_count,
        }}

    # Note: no /api/v1/session/start over HTTP. Starting a recording requires the
    # WebSocket, which is where grant verification and origin pinning live.

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        if not await runtime.ws.connect(websocket):
            return
        try:
            while True:
                raw = await websocket.receive_text()
                response = await runtime.ws.handle_message(websocket, raw)
                await websocket.send_json(response)
        except WebSocketDisconnect:
            await runtime.ws.disconnect(websocket)
        except Exception as exc:
            logger.error("WebSocket error: %s", exc, exc_info=True)
            await runtime.ws.disconnect(websocket)

    return app


def _error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "success": False,
            "error": {"code": code, "message": message},
            "timestamp": crypto.iso_utc(datetime.now(timezone.utc)),
        },
    )


__all__ = ["Runtime", "create_app"]
