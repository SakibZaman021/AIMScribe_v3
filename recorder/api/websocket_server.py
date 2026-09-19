"""
WebSocket control channel for the CMED browser.

This is the primary way CMED drives the recorder. The security model changed
completely from v1, which checked `websocket.client.host` and accepted anything
from 127.0.0.1 - that check always passes for *any* page open on the PC, because
the peer address of a browser is always loopback. WebSockets are also not subject
to CORS, so the wildcard CORS fix does not help here either.

What is enforced now, in order, before the socket is accepted:

1. `Origin` must be in the configured allowlist. Absent or "null" is rejected.
2. `Host` must be an expected loopback authority. This is the DNS-rebinding
   defence: `http://evil.example` can resolve to 127.0.0.1, and only the Host
   header distinguishes it.
3. The peer address must actually be loopback.

And before any recording starts, the `start` command must carry a CMED-signed,
single-use grant. Doctor, hospital and patient are read from that grant, never
from the browser's own payload.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket

from core import crypto
from core.crypto import GrantError
from core.session_controller import SessionError

logger = logging.getLogger(__name__)

# Close codes reported to the browser. 4403 is our "policy refused".
CLOSE_POLICY = 4403
CLOSE_INTERNAL = 4500

MAX_MESSAGE_BYTES = 64 * 1024


class GrantGuard:
    """
    Single-use enforcement for recording grants.

    A grant is a bearer token: without replay protection, a page that captured one
    could reopen sessions with it until it expired. Entries are pruned lazily.
    """

    def __init__(self) -> None:
        self._seen: Dict[str, float] = {}

    def consume(self, grant: crypto.Grant) -> None:
        now = time.time()
        if len(self._seen) > 512:
            self._seen = {jti: exp for jti, exp in self._seen.items() if exp > now}
        if self._seen.get(grant.jti, 0) > now:
            raise GrantError("grant has already been used")
        self._seen[grant.jti] = float(grant.expires_at)


class WebSocketManager:
    """Tracks connected CMED clients and dispatches their commands."""

    def __init__(self, cfg, *, controller=None, grant_guard: Optional[GrantGuard] = None):
        self.cfg = cfg
        self._controller = controller
        self._guard = grant_guard or GrantGuard()
        self._connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._grant_key = None
        self._uploader = None
        self._hospital_id = ""

    # ---- wiring ----

    def set_controller(self, controller) -> None:
        self._controller = controller

    def set_register_source(self, uploader, hospital_id: str) -> None:
        """Where the doctor list comes from: the backend, via the uploader's
        device-authenticated client, for this machine's hospital."""
        self._uploader = uploader
        self._hospital_id = hospital_id or ""

    def set_grant_key(self, key) -> None:
        self._grant_key = key

    @property
    def client_count(self) -> int:
        return len(self._connections)

    # ---- connection ----

    async def connect(self, websocket: WebSocket) -> bool:
        origin = websocket.headers.get("origin")
        host = websocket.headers.get("host")
        peer = websocket.client.host if websocket.client else None

        if not self.cfg.security.origin_allowed(origin):
            logger.warning("Rejected WebSocket: origin %r is not allowed", origin)
            await websocket.close(code=CLOSE_POLICY, reason="origin not allowed")
            return False

        if not self.cfg.security.host_allowed(host):
            logger.warning("Rejected WebSocket: host %r is not allowed (possible DNS rebinding)",
                           host)
            await websocket.close(code=CLOSE_POLICY, reason="host not allowed")
            return False

        if peer not in ("127.0.0.1", "::1"):
            logger.warning("Rejected WebSocket from non-loopback peer %r", peer)
            await websocket.close(code=CLOSE_POLICY, reason="loopback only")
            return False

        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info("CMED connected from %s (%s client(s))", origin, len(self._connections))

        await self._send(websocket, self._status_event())
        return True

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        logger.info("CMED disconnected (%s client(s) remain)", len(self._connections))

    # ---- dispatch ----

    async def handle_message(self, websocket: WebSocket, raw: str) -> Dict[str, Any]:
        if len(raw) > MAX_MESSAGE_BYTES:
            return self._error("message too large")

        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return self._error("invalid JSON")

        if not isinstance(message, dict):
            return self._error("expected a JSON object")

        command = str(message.get("command", "")).lower()
        if self._controller is None:
            return self._error("recorder is not ready")

        handlers = {
            "start": self._start,
            "stop": self._stop,
            "pause": self._pause,
            "resume": self._resume,
            "status": self._status,
            "doctors": self._doctors,
        }
        handler = handlers.get(command)
        if handler is None:
            return self._error(f"unknown command: {command or '(none)'}")

        try:
            return await handler(message)
        except SessionError as exc:
            # Expected refusals: safe to show the doctor verbatim.
            logger.info("Command %s refused: %s", command, exc)
            return self._error(str(exc), code="refused")
        except GrantError as exc:
            logger.warning("Command %s rejected: %s", command, exc)
            return self._error("Authorisation failed. Reload CMED and try again.",
                               code="unauthorised")
        except Exception as exc:
            logger.error("Command %s failed: %s", command, exc, exc_info=True)
            return self._error("The recorder hit an internal error.", code="internal")

    # ---- commands ----

    async def _start(self, message: Dict[str, Any]) -> Dict[str, Any]:
        grant_token = message.get("grant")

        if self.cfg.security.require_grant:
            if self._grant_key is None:
                raise GrantError("no grant verification key is installed")
            grant = crypto.verify_grant(
                grant_token,
                self._grant_key,
                issuer=self.cfg.security.grant_issuer,
                audience=self.cfg.security.grant_audience,
            )
            self._guard.consume(grant)
        else:
            # Development only; config.production_warnings() surfaces this loudly.
            session = message.get("session", {}) or {}
            logger.warning("Starting a session WITHOUT a grant (development mode)")
            grant = crypto.Grant(
                jti=f"dev-{time.time()}",
                doctor_id=str(session.get("doctor_id", "DR_DEV")),
                doctor_name=str(session.get("doctor_name", "")),
                hospital_id=str(session.get("hospital_id", "HOSP_DEV")),
                patient_ref=str(session.get("patient_id") or session.get("patient_ref") or ""),
                consent_obtained=True,
                consent_method="development",
                expires_at=int(time.time()) + 60,
                raw="",
            )
            if not grant.patient_ref:
                raise SessionError("A patient reference is required.")

        # Display-only fields may come from the browser; identity may not.
        patient_name = str((message.get("session") or {}).get("patient_name", ""))[:120]

        result = await self._controller.open_session(grant, patient_name=patient_name)
        return self._ack("start", result)

    async def _stop(self, message: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._controller.stop_session(reason="doctor_stopped")
        return self._ack("stop", result)

    async def _pause(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Supervised pause. Reason is mandatory; long pauses need a supervisor."""
        result = await self._controller.pause_session(
            reason=str(message.get("reason", "")),
            reason_detail=str(message.get("reason_detail", ""))[:500],
            authorised_by=str(message.get("authorised_by", ""))[:120],
            expected_seconds=int(message.get("expected_seconds", 0) or 0),
        )
        return self._ack("pause", result)

    async def _resume(self, message: Dict[str, Any]) -> Dict[str, Any]:
        result = await self._controller.resume_session()
        return self._ack("resume", result)

    async def _status(self, message: Dict[str, Any]) -> Dict[str, Any]:
        return self._status_event()

    async def _doctors(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Doctors seen at this hospital before, as typing suggestions only.

        Not a permission list. CMED decides who is consulting - doctors log in
        there and it knows the rota - so a name missing from this list is no
        reason to refuse a recording. An empty list is perfectly normal at a new
        site and must never block the clinic.
        """
        register = None
        if self._uploader is not None and self._hospital_id:
            register = await self._uploader.fetch_doctors(self._hospital_id)

        return self._ack("doctors", {
            "hospital_id": self._hospital_id or None,
            # Deliberately absent: the machine has no doctor.
            "doctors": register or [],
        })

    # ---- outbound ----

    def _status_event(self) -> Dict[str, Any]:
        status = self._controller.status() if self._controller else {"state": "starting"}
        return self._stamp({"event": "status", **status,
                            "connected_clients": len(self._connections)})

    async def send_event(self, event_type: str, data: Dict[str, Any]) -> None:
        await self.broadcast(self._stamp({"event": event_type, **data}))

    async def broadcast(self, message: Dict[str, Any]) -> None:
        """
        Fan out to every client concurrently.

        v1 awaited each send while holding the connection lock, so one wedged
        client blocked every broadcast and every connect. The set is snapshotted
        instead, and sends run in parallel outside the lock.
        """
        async with self._lock:
            targets = list(self._connections)
        if not targets:
            return

        results = await asyncio.gather(
            *(self._send(socket, message) for socket in targets),
            return_exceptions=True,
        )
        dead = {socket for socket, outcome in zip(targets, results)
                if isinstance(outcome, Exception) or outcome is False}
        if dead:
            async with self._lock:
                self._connections -= dead

    @staticmethod
    async def _send(websocket: WebSocket, message: Dict[str, Any]) -> bool:
        try:
            await websocket.send_json(message)
            return True
        except Exception:
            return False

    # ---- helpers ----

    @staticmethod
    def _stamp(payload: Dict[str, Any]) -> Dict[str, Any]:
        payload.setdefault("timestamp", crypto.iso_utc(datetime.now(timezone.utc)))
        return payload

    def _ack(self, command: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Reply to the sender only.

        State changes reach every client as broadcast events emitted by the
        controller. Commands previously broadcast *and* returned the same object,
        so the caller saw each state change two or three times and could not tell
        an acknowledgement from an event.
        """
        return self._stamp({"event": "ack", "command": command, "data": data})

    def _error(self, message: str, *, code: str = "error") -> Dict[str, Any]:
        return self._stamp({"event": "error", "code": code, "message": message})


__all__ = ["WebSocketManager", "GrantGuard", "CLOSE_POLICY"]
