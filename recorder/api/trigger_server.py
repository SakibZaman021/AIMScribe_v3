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

import json
import logging
import secrets
import time
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

# What register_from_trigger's answer starts with when the server could not be
# reached - not a refusal: the start is tried again until the internet is back.
OFFLINE = "offline: "

# The rate-limit bucket HTTP starts share (each WebSocket has its own).
_HTTP_CLIENT = object()


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

# A new PC with a TPM key and no token: not a problem, it joins at its first
# consultation. Shown as a warning, so the tray does not say BLOCKED.
AWAITING_REGISTRATION = ("This PC joins AIMS LAB by itself the first time CMED opens a "
                         "patient on it; it needs the internet at that moment, once.")
# How long a CMED click waits for this PC to register before it is told why not.
REGISTER_WAIT_SECONDS = 8.0

NOT_ENROLLED = ("This device is not enrolled. An administrator must install it with "
                "-EnrollmentToken, or place enrollment.token in the state folder.")

# A PC installed while the clinic's internet was down tries its token again this
# often, instead of waiting for someone to log off and on.
ENROL_RETRY_SECONDS = 60

# A PC that has not joined yet has no heartbeat to bring CMED's addresses, yet
# its first API 1 carries one: it asks the server for the list this often, and
# at once (no more than this often) when a page it does not know knocks.
ORIGINS_REFRESH_SECONDS = 60
ORIGINS_KNOCK_SECONDS = 10

class Runtime:
    """
    Owns every long-lived object the agent needs.

    Constructed by main.py, started and stopped by the FastAPI lifespan so that
    everything async lives on one event loop.
    """

    def __init__(self, cfg, *, log_salt: bytes):
        self.cfg = cfg
        self.log_salt = log_salt
        self.device_key = crypto.load_device_key(cfg)
        self.spool = Spool.from_config(cfg)
        self.ws = WebSocketManager(cfg, grant_guard=GrantGuard())
        self.uploader: Optional[UploadManager] = None
        self.controller: Optional[SessionController] = None
        self.identity = None
        self.problems = cfg.validate()
        self.warnings = cfg.production_warnings()
        self._loop = None
        self._enrol_task = None
        # The on-screen Stop and Pause control, when a screen is present.
        self.overlay = None

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
        # CMED's EHR addresses, kept by the server and signed with the same key
        # under their own domain: loaded from disk now, renewed by the heartbeat.
        from core import trusted_origins
        trusted_origins.configure(self.cfg.paths.state_dir, receipt_key)

        # The AIMS LAB server's grant key, pinned at install (SRS-GRT-04).
        grant_key = None
        if self.cfg.security.require_grant:
            try:
                grant_key = crypto.load_public_key(self.cfg.security.grant_public_key_path)
            except Exception as exc:
                logger.critical("Grant key unavailable (%s); no recording can be authorised, "
                                "so every recording is held on this PC until it is", exc)

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

        from core.enrollment import can_self_register, read_pending_token
        self_register = (self.identity is None and not read_pending_token(self.cfg)
                         and can_self_register(self.cfg, self.device_key))
        if self_register:
            self.warnings.append(AWAITING_REGISTRATION)
        elif self.identity is None:
            self.problems.append(NOT_ENROLLED)

        from core.enrollment import load_device_token

        self.uploader = UploadManager(
            self.cfg,
            device_key=self.device_key,
            spool=self.spool,
            receipt_public_key=receipt_key,
            on_event=self._on_event,
        )
        self.uploader.set_device_token(load_device_token(self.cfg))
        if self.identity is not None:
            self.uploader.device_id = self.identity.device_id
        self.uploader.set_grant_key(grant_key)
        from core.health_log import FILE_NAME as HEALTH_LOG, HealthLog
        self.uploader.health = HealthLog(self.cfg.paths.state_dir / HEALTH_LOG)
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

        if self_register:
            self.controller.register = self.register_from_trigger
        await self.controller.start()
        self.ws.set_controller(self.controller)
        self.ws.set_register_source(self.uploader, self.controller.hospital_id)

        from core.enrollment import read_pending_token
        if self.identity is None and read_pending_token(self.cfg):
            self._enrol_task = asyncio.create_task(self._enrol_until_done())
        self._origins_task = asyncio.create_task(self._fetch_origins_until_joined())

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

    async def _enrol_until_done(self) -> None:
        """
        Keep trying the staged token until the server answers. Without this a PC
        installed while the network was down stayed unable to record until the
        next logon, which on a clinic PC can be the next morning.
        """
        import asyncio
        from core.enrollment import EnrollmentError, enroll, load_device_token, \
            read_pending_token
        from core.uploader import build_ssl_context

        while self.identity is None:
            await asyncio.sleep(ENROL_RETRY_SECONDS)
            token = read_pending_token(self.cfg)
            if not token:
                return
            try:
                identity = await enroll(self.cfg, self.device_key, token,
                                        ssl_context=build_ssl_context(self.cfg))
            except EnrollmentError as exc:
                if exc.rejected:
                    logger.critical("Enrolment refused by the server, not retrying: %s", exc)
                    return
                logger.warning("Enrolment still waiting for the server: %s", exc)
                continue
            self.adopt_identity(identity, load_device_token(self.cfg))

    async def fetch_trusted_origins(self) -> bool:
        """
        CMED's addresses from the server, without a device token: what a PC that
        has not joined yet needs before its first API 1. Taken only when signed
        and newer, exactly as from a heartbeat. True when a new list came in.
        """
        import aiohttp
        from core import trusted_origins
        from core.uploader import build_ssl_context

        self._origins_asked = time.monotonic()
        ssl_context = build_ssl_context(self.cfg)
        timeout = aiohttp.ClientTimeout(total=self.cfg.backend.request_timeout)
        connector = aiohttp.TCPConnector(ssl=ssl_context) if ssl_context else None
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(self.cfg.backend.url("/trusted-origins"),
                                       params={"have": str(trusted_origins.version())}) as reply:
                    if reply.status != 200:
                        logger.info("The server did not send CMED's addresses (%s)",
                                    reply.status)
                        return False
                    data = await reply.json(content_type=None)
        except Exception as exc:
            logger.info("Could not ask the server for CMED's addresses: %s", exc)
            return False
        return isinstance(data, dict) and trusted_origins.apply(data.get("trusted_origins"))

    async def _fetch_origins_until_joined(self) -> None:
        """At start-up, then every minute until the heartbeat takes over."""
        import asyncio
        while True:
            await self.fetch_trusted_origins()
            if self.identity is not None:
                return
            await asyncio.sleep(ORIGINS_REFRESH_SECONDS)

    def unknown_origin_knocked(self) -> None:
        """
        A page this PC does not trust yet called. The address may have been
        added on the server a moment ago: ask now, so CMED's retry works,
        rather than at the next minute or heartbeat.
        """
        if self._loop is None:
            return
        if time.monotonic() - getattr(self, "_origins_asked", -1e9) < ORIGINS_KNOCK_SECONDS:
            return
        self._origins_asked = time.monotonic()
        try:
            self._loop.call_soon_threadsafe(self._keep, self.fetch_trusted_origins)
        except RuntimeError:
            pass

    async def register_from_trigger(self, cmed_hospital_id: str) -> Optional[str]:
        """
        A new PC's first CMED consultation: join at the clinic CMED named.
        Returns None once registered, else why not, for the doctor's screen.
        One registration at a time; a click that arrives meanwhile waits for it.
        """
        import asyncio
        from core.enrollment import EnrollmentError, load_device_token, register
        from core.uploader import build_ssl_context

        lock = self.__dict__.setdefault("_register_lock", asyncio.Lock())
        async with lock:
            if self.identity is not None:
                return None
            try:
                identity = await asyncio.wait_for(
                    register(self.cfg, self.device_key, cmed_hospital_id,
                             ssl_context=build_ssl_context(self.cfg)),
                    timeout=REGISTER_WAIT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("Registration did not finish in %.0f s; tried again "
                               "shortly", REGISTER_WAIT_SECONDS)
                return OFFLINE + "the AIMS LAB server did not answer in time"
            except EnrollmentError as exc:
                logger.error("Registration failed: %s", exc)
                # A refusal names its code; anything else is the network.
                return exc.code or OFFLINE + "the AIMS LAB server could not be reached"
            self.adopt_identity(identity, load_device_token(self.cfg))
            if self.controller is not None:
                self.controller.register = None
            return None

    def adopt_identity(self, identity, device_token: Optional[str]) -> None:
        """Enrolled while running: from now on this PC records and uploads."""
        self.identity = identity
        if self.uploader is not None:
            self.uploader.set_device_token(device_token)
            self.uploader.device_id = identity.device_id
        if self.controller is not None:
            self.controller.device_id = identity.device_id
            self.controller.hospital_id = identity.hospital_id
            self.controller.doctor_id = identity.doctor_id
            self.ws.set_register_source(self.uploader, identity.hospital_id)
        if NOT_ENROLLED in self.problems:
            self.problems.remove(NOT_ENROLLED)
        if AWAITING_REGISTRATION in getattr(self, "warnings", ()):
            self.warnings.remove(AWAITING_REGISTRATION)
        logger.info("Enrolled as device %s at %s; ready to record",
                    identity.device_id, identity.hospital_id)
        if self.uploader is not None:
            self.uploader.nudge()

    async def shutdown(self) -> None:
        for name in ("_enrol_task", "_origins_task"):
            task = getattr(self, name, None)
            if task is not None:
                task.cancel()
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
        if self.overlay is not None:
            self.overlay.post(event, data)
        if self._loop is None:
            return
        # Every alert goes to the health log first, then to the server at once
        # if it can be reached; otherwise it waits there for the network.
        uploader = getattr(self, "uploader", None)
        if event == "integrity_alert" and uploader is not None and uploader.health is not None:
            uploader.health.alert(data)
            try:
                self._loop.call_soon_threadsafe(self._keep, uploader.deliver_health)
            except RuntimeError:
                pass
        try:
            self._loop.call_soon_threadsafe(self._keep, lambda: self.ws.send_event(event, data))
        except RuntimeError:
            pass

    def _keep(self, make) -> None:
        """
        Start a task on the loop and hold it until it finishes. A task nobody
        references can be collected mid-flight, and its error is never seen.
        """
        task = self._loop.create_task(make())
        tasks = self.__dict__.setdefault("_tasks", set())
        tasks.add(task)

        def done(t) -> None:
            tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("Background event delivery failed: %s", t.exception())
        task.add_done_callback(done)

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
    # The list is live: CMED's addresses arrive from the server after start-up.
    class LiveOriginCORS(CORSMiddleware):
        def is_allowed_origin(self, origin: str) -> bool:
            return cfg.security.origin_allowed(origin)

    app.add_middleware(
        LiveOriginCORS,
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
            knocked = getattr(runtime, "unknown_origin_knocked", None)
            if knocked is not None:
                knocked()
            return _error_response(403, "FORBIDDEN_ORIGIN", "Origin not allowed")

        from api.websocket_server import _same_windows_session
        if not _same_windows_session(request):
            logger.warning("Rejected request from another Windows user's session")
            return _error_response(403, "OTHER_WINDOWS_SESSION",
                                   "This recorder belongs to the user signed in on this "
                                   "screen. Sign in as that user, or switch to their session.")

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

    @app.post("/api/v1/session/start")
    async def session_start(request: Request):
        """
        API 1 over plain HTTP, for a page or a test tool that cannot hold a
        WebSocket (Postman's collections cannot carry one). The same message
        as the WebSocket's `start` - {"request_id", "trigger": {...}} - goes
        through the same handler, so the checks, the grant, the recording and
        the reply are exactly the WebSocket's; the HTTP status is the reply's.

        The origin pinning the WebSocket does is done here too, and stricter
        than the middleware: a request with no Origin is refused, so only a
        registered page (or a tool that names one) can start a recording.
        """
        origin = request.headers.get("origin")
        if not origin or not cfg.security.origin_allowed(origin):
            logger.warning("Rejected HTTP start from origin %r", origin)
            return _error_response(403, "ORIGIN_NOT_ALLOWED",
                                   "This page is not allowed to control the recorder.")
        raw = (await request.body()).decode("utf-8", errors="replace")
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            message = None
        if isinstance(message, dict):
            raw = json.dumps({**message, "command": "start"})
        reply = await runtime.ws.handle_message(_HTTP_CLIENT, raw)
        return JSONResponse(status_code=int(reply.get("status") or 200), content=reply)

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
