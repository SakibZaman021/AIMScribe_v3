"""
Segment upload manager.

Drains the spool to the AIMS LAB server, in order, one segment at a time per
session, and never gives up on a segment. Replaces v1's `clip_uploader.py` (which
dropped clips when its queue filled and deleted local files on an HTTP 200) and
`file_forwarder.py` (which posted whole recordings to an unauthenticated endpoint).

Per segment the sequence is:

    POST /segment/authorize   -> presigned PUT url, short lived
    PUT  <presigned url>      -> the WAV bytes, straight to object storage
    POST /segment/commit      -> server re-reads the object and verifies sha256

Then, separately and later:

    GET  /session/{id}/receipts -> signed proof the archive copy exists
    verify signature + sha256   -> only now may the local file be deleted

Uploads and deletion are deliberately decoupled: a segment reaching storage is not
grounds for deleting the only other copy of it.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from core import crypto, spool as spool_mod
from core.crypto import ReceiptError
from core.spool import COMMITTED, PENDING, RECEIPTED, SessionSpool, Spool

logger = logging.getLogger(__name__)


@dataclass
class UploadOutcome:
    session_id: str
    seq_no: int
    ok: bool
    object_key: Optional[str] = None
    error: str = ""


def build_ssl_context(cfg) -> Optional[ssl.SSLContext]:
    """
    TLS 1.2 minimum, pinned CA when configured, client certificate for mTLS.

    Shared with enrollment, which runs before the upload manager exists but must
    use identical transport settings.
    """
    if not cfg.backend.uses_tls:
        return None

    ca = cfg.backend.ca_bundle_path
    context = ssl.create_default_context(cafile=str(ca) if ca and ca.is_file() else None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED

    cert, key = cfg.backend.client_cert_path, cfg.backend.client_key_path
    if cert and cert.is_file():
        context.load_cert_chain(str(cert), str(key) if key and key.is_file() else None)
    else:
        logger.warning("No client certificate configured; mTLS is not in use")
    return context


class UploadManager:
    """
    Owns the HTTP session and works through every spooled consultation.

    Recovered sessions are drained before the live one, so a backlog from an
    outage clears in the order it was recorded.
    """

    def __init__(
        self,
        cfg,
        *,
        device_key: crypto.DeviceKey,
        spool: Spool,
        receipt_public_key,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.cfg = cfg
        self.spool = spool
        self._device_key = device_key
        self._receipt_key = receipt_public_key
        self._on_event = on_event
        # Bearer credential issued at enrollment. Render terminates TLS itself and
        # offers no client certificates, so this is the transport identity.
        self._device_token: Optional[str] = None
        self._doctors_cache: Optional[List[Dict[str, Any]]] = None
        # Sessions already reported as unable to complete, so the
        # warning is raised once rather than on every tick.
        self._reported_quarantines: set = set()

        self._sessions: List[SessionSpool] = []
        self._http: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()
        self._running = False
        self._lock = asyncio.Lock()

        self.last_success_at: Optional[float] = None
        self.last_error: str = ""
        self.consecutive_failures = 0

    # ---- lifecycle ----

    def set_device_token(self, token: Optional[str]) -> None:
        """Set before start(); it becomes a default header on the HTTP session."""
        self._device_token = token

    async def start(self) -> None:
        if self._running:
            return

        if not self._device_token:
            logger.warning(
                "No device token available; backend calls will be rejected until "
                "this machine is enrolled")

        # The token is deliberately NOT a session default header. Segment uploads
        # use a presigned URL that points at Cloudflare R2, not at our backend, and
        # a default header would send our credential to a third-party host on every
        # upload. _request() attaches it per call; _put_object() never does.
        self._http = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(
                limit=4, limit_per_host=4, ssl=self._ssl_context()),
            headers={
                "User-Agent": f"AIMScribe/{self.cfg.app_version}",
                "X-AIMScribe-Protocol": str(self.cfg.protocol_version),
            },
        )
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="UploadManager")
        logger.info("Upload manager started; backend %s", self.cfg.backend.base_url)

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._http:
            await self._http.close()
            self._http = None
        logger.info("Upload manager stopped")

    def _ssl_context(self) -> Optional[ssl.SSLContext]:
        return build_ssl_context(self.cfg)

    # ---- registration ----

    async def track(self, session: SessionSpool) -> None:
        async with self._lock:
            if all(existing.session_id != session.session_id for existing in self._sessions):
                self._sessions.append(session)
        self.nudge()

    def nudge(self) -> None:
        """Ask the worker to run now rather than waiting for its next tick."""
        self._wake.set()

    # ---- worker ----

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Upload loop error: %s", exc, exc_info=True)

            try:
                await asyncio.wait_for(self._wake.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _drain_once(self) -> None:
        async with self._lock:
            sessions = list(self._sessions)

        for session in sessions:
            if not self._running:
                return

            if not session.server_acknowledged:
                if await self._open_remote(session):
                    session.mark_acknowledged()
                else:
                    # Backend unreachable. Segments stay sealed on disk; try again
                    # on the next tick. This is the normal outage path.
                    continue

            # A rejected clip stops this session dead, before anything else is
            # sent. It used to only stop the close, which was far too late:
            # quarantined segments drop out of the pending list, so the loop
            # sailed past the gap and uploaded the clips after it. Those arrived
            # with a prev_hash the backend could not follow, turning one rejected
            # clip into a broken chain.
            #
            # Nothing is lost by stopping. The audio stays sealed on disk and is
            # never purged without a receipt; it needs a human, not another
            # request every ten seconds.
            if session.quarantined_segments():
                if session.session_id not in self._reported_quarantines:
                    self._reported_quarantines.add(session.session_id)
                    seqs = [s.seq_no for s in session.quarantined_segments()]
                    logger.critical(
                        "Session %s cannot be completed: segment(s) %s were "
                        "rejected. Its audio is kept and will not be purged.",
                        session.session_id, seqs)
                    self._emit("integrity_alert", {
                        "session_id": session.session_id,
                        "alert_type": "session_needs_review",
                        "detail": f"quarantined segment(s) {seqs}; close cannot complete",
                    })
                continue

            # Strictly in chain order, and stop at the first failure.
            #
            # The chain is sequential: every entry's prev_hash is the previous
            # entry's hash, so an entry that arrives before its predecessor
            # cannot verify and the backend quarantines the whole session.
            #
            # Segments used to be the only thing retried here. Pause and resume
            # were sent once, best-effort, on the theory that the chain copy
            # would arrive at close - but the backend verifies each entry as it
            # lands, so a dropped pause broke every segment after it. A real
            # consultation was quarantined that way. Both kinds are delivered
            # here now, interleaved by entry number, which is the order the
            # chain was built in.
            #
            # Leaving the rest pending is free: everything is sealed on disk and
            # the next tick retries from whichever entry failed.
            work = [(s.entry_no, "segment", s) for s in session.pending_segments()]
            work += [(e.entry_no, "notify", e) for e in session.pending_notifications()]

            for entry_no, kind, item in sorted(work, key=lambda w: w[0]):
                if not self._running:
                    return
                if kind == "segment":
                    ok = (await self._send_segment(session, item)).ok
                else:
                    ok = await self._notify_chain_entry(session, item)
                if not ok:
                    logger.debug(
                        "Chain entry %s of %s did not land; holding the rest of "
                        "the session to preserve order", entry_no, session.session_id)
                    break

            # A session closed while the backend was unreachable still needs its
            # close delivered, or it would sit in 'open' forever and never be
            # archived. Retried here once every segment has landed.

            if (session.closed_at is not None
                    and not session.close_reported
                    and not session.pending_segments()
                    # Close verifies the whole chain, so it must not run while a
                    # pause is still undelivered.
                    and not session.pending_notifications()):
                await self.close_remote(
                    session,
                    duration_seconds=session.duration_seconds,
                    paused_seconds=session.paused_seconds,
                )

            await self._collect_receipts(session)
            self._purge_expired(session)

            if session.is_complete:
                async with self._lock:
                    self._sessions = [s for s in self._sessions
                                      if s.session_id != session.session_id]
                self.spool.discard(session)
                logger.info("Session %s fully archived and purged locally", session.session_id)

    # ---- protocol steps ----

    async def _open_remote(self, session: SessionSpool) -> bool:
        genesis = session.chain[0] if session.chain else None
        if genesis is None:
            logger.error("Session %s has no genesis chain entry", session.session_id)
            return False

        payload = {
            "session_id": session.session_id,
            "opened_at": crypto.iso_utc(session.opened_at) if session.opened_at else None,
            "doctor_id": session.meta.get("doctor_id"),
            "hospital_id": session.meta.get("hospital_id"),
            "patient_ref": session.meta.get("patient_ref"),
            "consent_obtained": True,
            "consent_method": session.meta.get("consent_method", ""),
            "audio": session.audio,
            "device_pubkey": self._device_key.public_bytes_raw().hex(),
            "genesis": genesis.to_wire(),
        }
        # attempts=1: the drain loop is itself the retry, every 10 seconds.
        # Retrying in here for the full backoff - 2+8+30+120+600, nearly
        # 13 minutes - blocks every other session behind this one, because
        # _drain_once works through them sequentially. One failing clip
        # stopped three later consultations from registering at all.
        result = await self._post("/session/open", payload, attempts=1)
        if result is None:
            return False
        logger.info("Session %s registered with the backend", session.session_id)
        self._emit("session_registered", {"session_id": session.session_id})
        return True

    async def _send_segment(self, session: SessionSpool, segment) -> UploadOutcome:
        seq_no = segment.seq_no
        segment.attempts += 1

        try:
            audio = session.read_segment(seq_no)
        except Exception as exc:
            # The spool file is unreadable or fails its own hash check. Never
            # silently skip: quarantine so a human looks at it.
            logger.critical("Segment %s of %s is unreadable: %s", seq_no, session.session_id, exc)
            session.set_state(seq_no, spool_mod.QUARANTINED)
            self._emit("integrity_alert", {
                "session_id": session.session_id, "seq_no": seq_no,
                "alert_type": "local_segment_unreadable", "detail": str(exc),
            })
            return UploadOutcome(session.session_id, seq_no, False, error=str(exc))

        # attempts=1: the drain loop is itself the retry, every 10 seconds.
        # Retrying in here for the full backoff - 2+8+30+120+600, nearly
        # 13 minutes - blocks every other session behind this one, because
        # _drain_once works through them sequentially. One failing clip
        # stopped three later consultations from registering at all.
        authorization = await self._post("/segment/authorize", {
            "session_id": session.session_id,
            "seq_no": seq_no,
            "bytes": segment.byte_length,
            "sha256": segment.sha256.hex(),
        }, attempts=1)
        if not authorization:
            segment.last_error = "authorize failed"
            return UploadOutcome(session.session_id, seq_no, False, error=segment.last_error)

        upload_url = authorization.get("upload_url")
        object_key = authorization.get("object_key")
        if not upload_url or not object_key:
            segment.last_error = "authorize response incomplete"
            return UploadOutcome(session.session_id, seq_no, False, error=segment.last_error)

        if not await self._put_object(upload_url, audio):
            segment.last_error = "object upload failed"
            return UploadOutcome(session.session_id, seq_no, False, error=segment.last_error)

        chain_entry = next(
            (entry for entry in session.chain if entry.entry_no == segment.entry_no), None)
        commit = await self._post("/segment/commit", {
            "session_id": session.session_id,
            "seq_no": seq_no,
            "object_key": object_key,
            "sha256": segment.sha256.hex(),
            "bytes": segment.byte_length,
            "duration_seconds": segment.duration_seconds,
            "captured_start_at": crypto.iso_utc(segment.captured_start_at),
            "captured_end_at": crypto.iso_utc(segment.captured_end_at),
            "rms_mean": segment.rms_mean,
            "is_final": segment.is_final,
            "chain_entry": chain_entry.to_wire() if chain_entry else None,
        }, attempts=1)
        if not commit:
            segment.last_error = "commit failed"
            return UploadOutcome(session.session_id, seq_no, False, error=segment.last_error)

        if commit.get("status") == "quarantined":
            logger.critical("Backend quarantined segment %s of %s: %s",
                            seq_no, session.session_id, commit.get("reason"))
            session.set_state(seq_no, spool_mod.QUARANTINED, object_key=object_key)
            self._emit("integrity_alert", {
                "session_id": session.session_id, "seq_no": seq_no,
                "alert_type": "server_hash_mismatch", "detail": commit.get("reason", ""),
            })
            return UploadOutcome(session.session_id, seq_no, False, object_key, "quarantined")

        session.set_state(seq_no, COMMITTED, object_key=object_key)
        self.last_success_at = time.time()
        self.consecutive_failures = 0
        logger.info("Segment %s of %s committed", seq_no, session.session_id)
        self._emit("segment_committed", {
            "session_id": session.session_id,
            "seq_no": seq_no,
            "duration_seconds": segment.duration_seconds,
        })
        return UploadOutcome(session.session_id, seq_no, True, object_key)

    async def _notify_chain_entry(self, session: SessionSpool, entry) -> bool:
        """
        Deliver one pause or resume entry, and remember that it landed.

        Retried by the drain loop until it does. A pause is a link in the hash
        chain, so losing it is not a cosmetic loss of a dashboard update - it
        invalidates every entry recorded after it.
        """
        endpoint = "/session/pause" if entry.entry_type == "pause" else "/session/resume"
        result = await self._post(endpoint, {
            "session_id": session.session_id,
            "chain_entry": entry.to_wire(),
        }, attempts=1)
        if result is None:
            return False

        # The backend answers 200 with "deferred" when the entry does not follow
        # its stored head - it is holding the door open, not accepting the entry.
        # Treating that as delivered marked three real entries as sent, so they
        # were never retried and the session stayed broken.
        if result.get("status") != "recorded":
            logger.info("Entry %s of %s deferred by the backend: %s",
                        entry.entry_no, session.session_id, result.get("reason", ""))
            return False

        session.mark_entry_reported(entry.entry_no)
        return True

    async def notify_pause(self, session: SessionSpool, entry) -> bool:
        """
        Send the pause immediately so the dashboard reflects it now.

        One attempt, because this runs while the doctor is waiting. If it fails
        the drain loop retries it - which is the difference between a late
        dashboard update and a quarantined consultation.
        """
        return await self._notify_chain_entry(session, entry)

    async def notify_resume(self, session: SessionSpool, entry) -> bool:
        """As for notify_pause."""
        return await self._notify_chain_entry(session, entry)

    async def close_remote(
        self, session: SessionSpool, *, duration_seconds: float, paused_seconds: float
    ) -> bool:
        close_entry = session.chain[-1] if session.chain else None
        result = await self._post("/session/close", {
            "session_id": session.session_id,
            "closed_at": crypto.iso_utc(session.closed_at) if session.closed_at else None,
            "close_reason": session.close_reason,
            "duration_seconds": duration_seconds,
            "paused_seconds": paused_seconds,
            "segment_count": len(session.segments),
            "chain_head": session.head_hash.hex() if session.head_hash else None,
            "chain_entry": close_entry.to_wire() if close_entry else None,
            "manifest": session.manifest(),
        }, attempts=1)
        if result is None:
            return False

        status = result.get("status")

        # Not a failure and not a close: the server is still missing segments,
        # normally because close raced the final upload. Leaving close_reported
        # unset is the whole point - the drain loop retries once every segment has
        # landed. Marking it here stranded the session open permanently, with the
        # audio uploaded but never archived and never purged.
        if status == "incomplete":
            logger.info(
                "Close deferred for %s: backend holds %s of %s segments",
                session.session_id, result.get("server_segments"),
                result.get("agent_segments", len(session.segments)))
            return False

        if status == "quarantined":
            logger.critical("Backend quarantined session %s at close: %s",
                            session.session_id, result.get("reason"))
            self._emit("integrity_alert", {
                "session_id": session.session_id,
                "alert_type": "chain_rejected",
                "detail": result.get("reason", ""),
            })

        # Quarantined still counts as delivered: the server has accounted for the
        # session and a human will review it. What must not happen is retrying
        # close forever, or treating it as never sent.
        session.mark_close_reported()
        return True

    async def heartbeat(self, status: Dict[str, Any]) -> bool:
        return await self._post("/heartbeat", status, attempts=1) is not None

    # ---- receipts and deletion ----

    async def _collect_receipts(self, session: SessionSpool) -> None:
        """Fetch and verify purge receipts. A bad receipt is never acted on."""
        awaiting = [s for s in session.segments.values() if s.state == COMMITTED]
        if not awaiting:
            return

        result = await self._get(f"/session/{session.session_id}/receipts", attempts=1)
        if not result:
            return

        for item in result.get("receipts", []):
            payload = item.get("payload") or {}
            try:
                signature = bytes.fromhex(item.get("signature", ""))
            except ValueError:
                logger.warning("Malformed receipt signature for %s", session.session_id)
                continue

            scope = payload.get("scope")
            seq_no = payload.get("seq_no")
            if scope != "segment" or not isinstance(seq_no, int):
                continue
            segment = session.segments.get(seq_no)
            if not segment or segment.state != COMMITTED:
                continue

            try:
                crypto.verify_purge_receipt(
                    payload, signature, self._receipt_key,
                    expect_session_id=session.session_id,
                    expect_sha256=segment.sha256,
                    expect_scope="segment",
                    expect_seq_no=seq_no,
                )
            except ReceiptError as exc:
                # Either a bug on the server or someone forging permission to
                # destroy evidence. Keep the audio and raise it.
                logger.critical("Rejected purge receipt for %s segment %s: %s",
                                session.session_id, seq_no, exc)
                self._emit("integrity_alert", {
                    "session_id": session.session_id, "seq_no": seq_no,
                    "alert_type": "invalid_purge_receipt", "detail": str(exc),
                })
                continue

            session.record_receipt(seq_no, payload, signature)
            logger.info("Verified purge receipt for %s segment %s", session.session_id, seq_no)

    def _purge_expired(self, session: SessionSpool) -> None:
        """Delete receipted segments once the grace window has passed."""
        grace = self.cfg.spool.purge_grace_hours * 3600
        now = time.time()
        for segment in list(session.segments.values()):
            if segment.state != RECEIPTED:
                continue
            if segment.receipt_at and (now - segment.receipt_at) < grace:
                continue
            if session.purge_segment(segment.seq_no):
                self._emit("segment_purged", {
                    "session_id": session.session_id, "seq_no": segment.seq_no,
                })

    # ---- doctor register ----

    async def fetch_doctors(self, hospital_id: str) -> Optional[List[Dict[str, Any]]]:
        """
        The doctors credentialed to record at this hospital, for the CMED selector.

        Cached, and the cache is kept when the backend is unreachable: a shared
        consulting room offline for an afternoon still needs to name its doctor,
        and a stale list of colleagues is better than an empty one. One attempt
        only - the page is waiting, and there is a cache to fall back on.
        """
        if not hospital_id:
            return None
        body = await self._get(f"/doctors?hospital_id={hospital_id}", attempts=1)
        if body is not None and isinstance(body.get("doctors"), list):
            self._doctors_cache = body["doctors"]
        return self._doctors_cache

    # ---- HTTP ----

    async def _post(
        self, endpoint: str, payload: Dict[str, Any], *, attempts: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        return await self._request("POST", endpoint, json_body=payload, attempts=attempts)

    async def _get(
        self, endpoint: str, *, attempts: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        return await self._request("GET", endpoint, attempts=attempts)

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        attempts: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Call the backend with bounded retries.

        Returns None when every attempt failed. Callers treat None as "try again
        later" and leave the segment sealed on disk - nothing is ever discarded
        because a request failed.
        """
        if self._http is None:
            return None

        backoff = self.cfg.backend.retry_backoff
        total = attempts if attempts is not None else len(backoff) + 1
        url = self.cfg.backend.url(endpoint)
        timeout = aiohttp.ClientTimeout(total=self.cfg.backend.request_timeout)

        # Attached here, and only here: these requests go to our own backend.
        headers = {"X-Device-Token": self._device_token} if self._device_token else None

        for attempt in range(total):
            try:
                async with self._http.request(
                    method, url, json=json_body, headers=headers, timeout=timeout
                ) as response:
                    if response.status < 300:
                        if response.content_type == "application/json":
                            return await response.json()
                        return {}

                    body = (await response.text())[:400]

                    # 4xx other than 408/429 will not improve with retries.
                    if 400 <= response.status < 500 and response.status not in (408, 429):
                        logger.error("%s %s rejected: %s %s", method, endpoint,
                                     response.status, body)
                        self.last_error = f"{response.status} {body}"
                        return None

                    logger.warning("%s %s failed: %s %s", method, endpoint,
                                   response.status, body)
                    self.last_error = f"{response.status}"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("%s %s error: %s", method, endpoint, self.last_error)

            if attempt < total - 1:
                await asyncio.sleep(backoff[min(attempt, len(backoff) - 1)])

        self.consecutive_failures += 1
        return None

    async def _put_object(self, url: str, data: bytes) -> bool:
        """PUT straight to object storage using the presigned URL."""
        if self._http is None:
            return False

        backoff = self.cfg.backend.retry_backoff
        timeout = aiohttp.ClientTimeout(total=self.cfg.backend.upload_timeout)

        for attempt in range(len(backoff) + 1):
            try:
                async with self._http.put(
                    url, data=data,
                    headers={"Content-Type": "audio/wav"},
                    timeout=timeout,
                ) as response:
                    if response.status < 300:
                        return True
                    body = (await response.text())[:300]
                    logger.warning("Object PUT failed: %s %s", response.status, body)
                    # An expired presigned URL must be re-requested, not retried.
                    if response.status in (400, 403):
                        return False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Object PUT error: %s", exc)

            if attempt < len(backoff):
                await asyncio.sleep(backoff[attempt])

        return False

    # ---- events ----

    def _emit(self, event: str, data: Dict[str, Any]) -> None:
        if self._on_event:
            try:
                self._on_event(event, data)
            except Exception as exc:
                logger.debug("Event handler for %s raised: %s", event, exc)

    # ---- status ----

    def status(self) -> Dict[str, Any]:
        pending = sum(len(s.pending_segments()) for s in self._sessions)
        return {
            "tracked_sessions": len(self._sessions),
            "pending_segments": pending,
            "spool_bytes": self.spool.total_bytes(),
            "spool_pressure": self.spool.pressure(),
            "last_success_at": self.last_success_at,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "online": self.consecutive_failures == 0 and self.last_success_at is not None,
        }


__all__ = ["UploadManager", "UploadOutcome", "build_ssl_context"]
