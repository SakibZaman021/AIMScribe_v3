"""
WebSocket control channel for the CMED page (Channel A, SRS 3.2 §6.1).

Admission is unchanged, and is checked before the socket is accepted, in order:

1. `Origin` must be in the configured allowlist. Absent or "null" is rejected.
2. `Host` must be an expected loopback authority. This is the DNS-rebinding
   defence: `http://evil.example` can resolve to 127.0.0.1, and only the Host
   header distinguishes it.
3. The peer address must actually be loopback.

What v3 changes is what the page sends. It no longer signs a grant or holds a
key: it sends five plain fields (API 1), and the recorder asks the AIMS LAB
server for the grant. The page cannot choose the clinic - that is this PC's
enrolment - and it cannot end a consultation early: a new trigger is refused
until `prescription_built` (API 3) has armed the gate.

Every command gets exactly one reply carrying `request_id`, `status` and `code`
(api/protocol.py). Pages act on `code`, never on `message`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, Set, Tuple

from fastapi import WebSocket

from api import protocol
from api.protocol import ProtocolError, Trigger
from core import crypto
# GrantGuard lives with the other grant code now; re-exported for older imports.
from core.crypto import GrantError, GrantGuard  # noqa: F401
from core.session_controller import SessionError

logger = logging.getLogger(__name__)

# Close codes reported to the browser. 4403 is our "policy refused".
CLOSE_POLICY = 4403
CLOSE_INTERNAL = 4500

MAX_MESSAGE_BYTES = 64 * 1024

class WebSocketManager:
    """Tracks connected CMED pages and answers their commands."""

    def __init__(self, cfg, *, controller=None, grant_guard: Optional[GrantGuard] = None):
        self.cfg = cfg
        self._controller = controller
        self._guard = grant_guard or GrantGuard()
        self._connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
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
            return self._stamp(protocol.reply("", "MALFORMED_MESSAGE",
                                              message="The message is too large."))
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return self._stamp(protocol.reply("", "MALFORMED_MESSAGE"))
        if not isinstance(message, dict):
            return self._stamp(protocol.reply("", "MALFORMED_MESSAGE",
                                              message="Expected a JSON object."))

        command = str(message.get("command", "")).lower()
        request_id = protocol.request_id_of(message)

        handler = self._handlers().get(command)
        if handler is None:
            return self._stamp(protocol.reply(
                command, "UNKNOWN_COMMAND", request_id=request_id,
                message=f"Unknown command: {command or '(none)'}"))
        if self._controller is None:
            return self._stamp(protocol.reply(command, "AGENT_NOT_READY",
                                              request_id=request_id))

        try:
            code, data = await handler(message)
            return self._stamp(protocol.reply(command, code, request_id=request_id, data=data))
        except ProtocolError as exc:
            logger.info("Command %s refused: %s", command, exc)
            return self._stamp(protocol.reply(command, exc.code, request_id=request_id,
                                              message=str(exc)))
        except SessionError as exc:
            # Expected refusals: the message is safe to show the doctor verbatim.
            logger.info("Command %s refused: %s", command, exc)
            code = self._contract_code(command, exc.code)
            return self._stamp(protocol.reply(command, code, request_id=request_id,
                                              message=str(exc)))
        except GrantError as exc:
            logger.warning("Command %s rejected: %s", command, exc)
            return self._stamp(protocol.reply(command, "AUTHORISATION_FAILED",
                                              request_id=request_id))
        except Exception as exc:
            logger.error("Command %s failed: %s", command, exc, exc_info=True)
            return self._stamp(protocol.reply(command, "AGENT_NOT_READY",
                                              request_id=request_id,
                                              message="The recorder hit an internal error."))

    def _handlers(self) -> Dict[str, Callable[[Dict[str, Any]],
                                              Awaitable[Tuple[str, Dict[str, Any]]]]]:
        return {
            "start": self._start,
            "prescription_built": self._prescription_built,
            "stop": self._stop,
            "pause": self._pause,
            "resume": self._resume,
            "status": self._status,
            "doctors": self._doctors,
        }

    @staticmethod
    def _contract_code(command: str, code: str) -> str:
        """Keep CMED's two commands inside Appendix A (SRS-IF1-10)."""
        if command in protocol.CMED_COMMANDS and code in ("OK", "REFUSED"):
            return "AGENT_NOT_READY"
        return code if code in protocol.CODES else "REFUSED"

    # ---- CMED's commands ----

    async def _start(self, message: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """
        API 1. Five fields in. The recorder starts at once and asks the AIMS LAB
        server for the grant alongside (SRS-GRT-07): 200 when the server has
        already said yes, 202 RECORDING_PROVISIONAL while it is still being asked.
        """
        trigger = protocol.parse_trigger(message)
        grant = None if self.cfg.security.require_grant else self._development_grant(trigger)
        result = await self._controller.open_session(trigger, grant=grant)
        code = ("RECORDING_STARTED" if result.get("authorisation") == "granted"
                else "RECORDING_PROVISIONAL")
        return code, {
            "session_id": result["session_id"],
            "started_at": result["started_at"],
            "armed": False,
            "supersedes": result.get("previous_session_id"),
            "authorisation": result.get("authorisation"),
            "confirmation": result.get("confirmation"),
        }

    @staticmethod
    def _development_grant(trigger: Trigger) -> crypto.Grant:
        """Development only; config.production_warnings() surfaces this loudly."""
        logger.warning("Starting a session WITHOUT server authorisation (development mode)")
        return crypto.Grant(
            jti=f"dev-{time.time_ns()}",
            doctor_id=trigger.doctor_id,
            doctor_name="",
            hospital_id="",          # the controller files under this PC's clinic
            patient_ref=trigger.patient_id,
            consent_obtained=True,
            consent_method="reception",
            expires_at=int(time.time()) + 60,
            raw="",
        )

    async def _prescription_built(self, message: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """API 3, part one. Arms the gate; the recording keeps running."""
        built = protocol.parse_prescription_built(message)
        result = await self._controller.arm(patient_id=built.patient_id,
                                            session_id=built.session_id)
        code = "GATE_ALREADY_ARMED" if result.get("already") else "GATE_ARMED"
        return code, {"session_id": result["session_id"], "armed": True}

    # ---- commands CMED does not use ----

    async def _stop(self, message: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        # The on-screen control sends the reason; "patient_did_not_consent"
        # erases the consultation (SRS 3.2 §7.8a).
        reason = str(message.get("reason") or "doctor_stopped")[:64]
        return "OK", await self._controller.stop_session(reason=reason)

    async def _pause(self, message: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Supervised pause. Reason is mandatory; long pauses need a supervisor."""
        return "OK", await self._controller.pause_session(
            reason=str(message.get("reason", "")),
            reason_detail=str(message.get("reason_detail", ""))[:500],
            authorised_by=str(message.get("authorised_by", ""))[:120],
            expected_seconds=int(message.get("expected_seconds", 0) or 0),
        )

    async def _resume(self, message: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        return "OK", await self._controller.resume_session()

    async def _status(self, message: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        return "OK", self._controller.status()

    async def _doctors(self, message: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """
        Doctors seen at this hospital before, as typing suggestions only.

        Not a permission list: CMED decides who is consulting. An empty list is
        normal at a new site and must never block the clinic.
        """
        register = None
        if self._uploader is not None and self._hospital_id:
            register = await self._uploader.fetch_doctors(self._hospital_id)
        return "OK", {"hospital_id": self._hospital_id or None, "doctors": register or []}

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

        The set is snapshotted and sends run in parallel outside the lock, so one
        wedged client cannot block every broadcast and every connect.
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


__all__ = ["WebSocketManager", "GrantGuard", "CLOSE_POLICY"]
