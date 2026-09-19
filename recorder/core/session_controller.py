"""
Session controller - the agent's state machine.

Owns one consultation at a time and coordinates capture, segmenting, the spool,
and the upload manager. Everything that changes a recording's state passes
through here so it can be written to the chain and the audit trail.

States:

    IDLE ──open──> RECORDING ──pause──> PAUSED ──resume──> RECORDING
                       │                   │
                       └──── stop ─────────┴──> CLOSING ──> IDLE

Rules enforced here rather than trusted from the caller:

* The hospital comes from the device enrolment; the doctor comes from a
  verified CMED grant and is checked against the backend's register.
* A session cannot open without recorded patient consent.
* Pause requires a reason from a fixed list, and a supervisor's name once the
  expected duration passes the configured threshold.
* Stopping never deletes audio. Local files go only when a signed purge receipt
  proves the archive copy exists, which the upload manager handles.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from core import crypto
from core.crypto import DeviceKey, Grant
from core.recorder import AudioCaptureError, AudioRecorder
from core.simple_splitter import SealedSegment, Segmenter
from core.spool import SessionSpool, Spool
from core.uploader import UploadManager

logger = logging.getLogger(__name__)

IDLE = "idle"
RECORDING = "recording"
PAUSED = "paused"
CLOSING = "closing"


class SessionError(RuntimeError):
    """A session request that must be refused, with a reason safe to show a user."""


@dataclass
class PauseRecord:
    reason: str
    reason_detail: str
    authorised_by: str
    supervisor_required: bool
    started_at: datetime
    started_monotonic: float


@dataclass
class ActiveSession:
    spool: SessionSpool
    grant: Grant
    patient_name: str
    recorder: AudioRecorder
    segmenter: Segmenter
    opened_at: datetime
    # Who is consulting. Usually the machine's own doctor, but a shared
    # consulting room rotates, so CMED may name someone else for this
    # consultation - the backend has already checked they are credentialed here.
    doctor_id: str = ""
    audio_seconds: float = 0.0
    paused_seconds: float = 0.0
    pause: Optional[PauseRecord] = None
    pauses: List[Dict[str, Any]] = field(default_factory=list)
    consecutive_silent: int = 0
    silence_alerted: bool = False

    @property
    def session_id(self) -> str:
        return self.spool.session_id


class SessionController:
    def __init__(
        self,
        cfg,
        *,
        device_key: DeviceKey,
        spool: Spool,
        uploader: UploadManager,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        log_salt: bytes = b"aimscribe",
    ):
        self.cfg = cfg
        self._device_key = device_key
        self._spool = spool
        self._uploader = uploader
        self._on_event = on_event
        self._log_salt = log_salt

        self.state = IDLE
        self._active: Optional[ActiveSession] = None
        self._lock = asyncio.Lock()
        # Strong references to fire-and-forget tasks. Without this the event loop
        # may garbage-collect a running task mid-flight.
        self._background: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        # Both set from the server-issued device identity at startup. Empty means
        # this machine is not enrolled and must not record.
        self.device_id: str = ""
        self.hospital_id: str = ""
        # Kept only so an old enrolment file still loads. Nothing reads it: a
        # PC does not have a doctor, and pretending otherwise is what filed
        # afternoon consultations under the morning shift.
        self.doctor_id: str = ""
        self.last_alert: str = ""

    # ---- startup / shutdown ----

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._recover_previous_sessions()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="Heartbeat")

    async def close(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        if self._active:
            await self.stop_session(reason="agent_shutdown")

    async def _recover_previous_sessions(self) -> None:
        """
        Adopt anything left in the spool by a previous run.

        A session that was still open when the process died is closed short and the
        interruption is recorded, so the gap is explained rather than mysterious.
        """
        for session in self._spool.recover(self._device_key):
            if session.closed_at is None:
                logger.warning("Session %s was interrupted; closing it short",
                               session.session_id)
                session.append_chain_entry("pause", crypto.pause_payload(
                    reason="non_clinical_interruption",
                    reason_detail="agent stopped unexpectedly; recovered at startup",
                    authorised_by="system",
                    supervisor_required=False,
                    at=datetime.now(timezone.utc),
                ))
                total = sum(s.duration_seconds for s in session.segments.values())
                session.close(duration_seconds=total, paused_seconds=0.0,
                              reason="recovered_after_unclean_shutdown")
                self._emit("integrity_alert", {
                    "session_id": session.session_id,
                    "alert_type": "unexpected_agent_exit",
                    "detail": "session recovered from the spool after an unclean shutdown",
                })
            await self._uploader.track(session)

    # ---- opening ----

    async def open_session(
        self,
        grant: Grant,
        *,
        patient_name: str = "",
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        async with self._lock:
            previous_id: Optional[str] = None

            # One instant for the whole handover. CMED treats consultations as
            # contiguous - the second patient's start time is the first
            # patient's end time - so both must be stamped from the same value.
            # Reading the clock twice put the microphone teardown and the final
            # segment seal in between, leaving a gap that made the two records
            # disagree.
            boundary = datetime.now(timezone.utc)

            if not self.device_id:
                raise SessionError(
                    "This PC is not enrolled with the AIMS LAB server. Contact IT - "
                    "recordings cannot be attributed or archived until it is.")

            # Who is consulting comes from CMED, every time, with no fallback.
            #
            # A consulting room runs two shifts. The morning doctors and the
            # afternoon doctors use the same laptops, and nobody knows in advance
            # which desk anyone will sit at. Taking the doctor from the enrolment
            # meant an afternoon consultation was filed under whoever happened to
            # be enrolled on that machine in the morning - silently, and in the
            # filename.
            #
            # CMED knows who is on shift: doctors log in there, and it sends the
            # doctor with the trigger. Falling back to the machine's own doctor
            # is precisely the bug, so there is no fallback. A trigger that names
            # nobody is refused rather than guessed at.
            doctor_id = grant.doctor_id
            if not doctor_id:
                raise SessionError(
                    "CMED did not say which doctor is seeing this patient, so the "
                    "recording cannot be attributed. Start the consultation from "
                    "CMED rather than starting the recorder directly.")

            # The hospital also comes from CMED, because a doctor can be moved to
            # another site at a day's notice. The enrolment is still what decides
            # whether this machine may record at all - an unenrolled laptop gets
            # nowhere - but it no longer decides the label. A disagreement is
            # worth knowing about, so it is raised without blocking the clinic.
            hospital_id = grant.hospital_id or self.hospital_id
            if grant.hospital_id and self.hospital_id and grant.hospital_id != self.hospital_id:
                self._emit("integrity_alert", {
                    "session_id": None,
                    "alert_type": "hospital_mismatch",
                    "detail": (f"device enrolled at {self.hospital_id}, "
                               f"CMED says {grant.hospital_id}"),
                })

            if self._active is not None:
                # A doctor opening a different patient means the previous
                # consultation is over. Close it properly rather than abandoning it.
                previous_id = self._active.session_id
                logger.info("Closing session %s before opening a new one", previous_id)
                await self._close_active(reason="superseded_by_new_patient", at=boundary)

            if not self._spool.has_capacity(self.cfg.audio.bytes_per_second * 240):
                raise SessionError(
                    "Local audio buffer is full. Recording cannot start until the "
                    "backlog uploads. Contact support.")

            spool_session = self._spool.open_session(
                device_key=self._device_key,
                device_id=self.device_id,
                doctor_id=doctor_id,
                hospital_id=hospital_id,
                patient_ref=grant.patient_ref,
                consent_method=grant.consent_method,
                audio={
                    "sample_rate": self.cfg.audio.sample_rate,
                    "channels": self.cfg.audio.channels,
                    "sample_width": self.cfg.audio.sample_width,
                },
                session_id=session_id,
            )

            segmenter = Segmenter(
                sample_rate=self.cfg.audio.sample_rate,
                channels=self.cfg.audio.channels,
                sample_width=self.cfg.audio.sample_width,
                min_seconds=self.cfg.segment.min_seconds,
                max_seconds=self.cfg.segment.max_seconds,
                grace_seconds=self.cfg.segment.grace_seconds,
                silence_rms=self.cfg.segment.silence_rms,
                silence_hold_seconds=self.cfg.segment.silence_hold_seconds,
                on_segment=self._on_segment_sealed,
            )
            recorder = AudioRecorder(
                sample_rate=self.cfg.audio.sample_rate,
                channels=self.cfg.audio.channels,
                sample_width=self.cfg.audio.sample_width,
                frames_per_buffer=self.cfg.audio.frames_per_buffer,
                input_device_index=self.cfg.audio.input_device_index,
                on_chunk=segmenter.submit,
                on_error=self._on_capture_error,
            )

            opened_at = boundary
            segmenter.start(opened_at)
            try:
                recorder.start()
            except AudioCaptureError as exc:
                segmenter.stop(seal_remaining=False)
                self._emit("integrity_alert", {
                    "session_id": spool_session.session_id,
                    "alert_type": "microphone_unavailable",
                    "detail": str(exc),
                })
                raise SessionError(
                    "The microphone is unavailable. Check that it is connected and "
                    "not in use by another application.") from exc

            self._active = ActiveSession(
                spool=spool_session,
                grant=grant,
                patient_name=patient_name,
                recorder=recorder,
                segmenter=segmenter,
                opened_at=opened_at,
                doctor_id=doctor_id,
            )
            self.state = RECORDING

            await self._uploader.track(spool_session)
            self._uploader.nudge()

            logger.info("Session %s opened for patient %s by doctor %s at %s",
                        spool_session.session_id,
                        self._pseudonym(grant.patient_ref),
                        self._pseudonym(doctor_id),
                        self.hospital_id)

            result = {
                "session_id": spool_session.session_id,
                "status": "recording",
                "started_at": crypto.iso_utc(opened_at),
                "previous_session_stopped": previous_id is not None,
                "previous_session_id": previous_id,
            }
            self._emit("recording_started", {
                "session_id": spool_session.session_id,
                "patient_ref": grant.patient_ref,
                "doctor_id": doctor_id,
                "hospital_id": self.hospital_id,
            })
            return result

    # ---- pause / resume ----

    async def pause_session(
        self,
        *,
        reason: str,
        reason_detail: str = "",
        authorised_by: str = "",
        expected_seconds: int = 0,
    ) -> Dict[str, Any]:
        """
        Supervised pause. The gap becomes an explained chain entry.

        Capture is stopped outright rather than muted, so the operating system's
        microphone indicator also goes out - the patient can see it has stopped.
        """
        async with self._lock:
            active = self._require_active()
            if self.state == PAUSED:
                raise SessionError("Recording is already paused.")

            if reason not in self.cfg.pause.reasons:
                raise SessionError(
                    f"Unknown pause reason. Choose one of: {', '.join(self.cfg.pause.reasons)}")
            if reason == "other" and not reason_detail.strip():
                raise SessionError("A written reason is required when choosing 'other'.")

            threshold = self.cfg.pause.self_authorise_seconds
            supervisor_required = expected_seconds > threshold
            if supervisor_required and not authorised_by.strip():
                raise SessionError(
                    f"A pause longer than {threshold // 60} minutes needs a "
                    "supervisor's name.")

            # Order matters: stop capture first so no further chunks can be queued,
            # then flush. Flushing first would let audio recorded after the pause
            # decision land in the next segment, blurring the boundary the chain
            # entry claims is exact.
            stats = active.recorder.stop()
            active.audio_seconds += stats.bytes_captured / max(1, active.recorder.bytes_per_second)
            active.segmenter.flush(is_final=False)

            now = datetime.now(timezone.utc)
            entry = active.spool.append_chain_entry("pause", crypto.pause_payload(
                reason=reason,
                reason_detail=reason_detail,
                authorised_by=authorised_by or active.doctor_id,
                supervisor_required=supervisor_required,
                at=now,
            ))
            active.pause = PauseRecord(
                reason=reason,
                reason_detail=reason_detail,
                authorised_by=authorised_by or active.doctor_id,
                supervisor_required=supervisor_required,
                started_at=now,
                started_monotonic=time.monotonic(),
            )
            self.state = PAUSED

            self._spawn(self._uploader.notify_pause(active.spool, entry))
            self._uploader.nudge()

            logger.warning("Session %s PAUSED: reason=%s authorised_by=%s",
                           active.session_id, reason,
                           self._pseudonym(active.pause.authorised_by))
            self._emit("recording_paused", {
                "session_id": active.session_id,
                "reason": reason,
                "reason_detail": reason_detail,
                "authorised_by": active.pause.authorised_by,
                "paused_at": crypto.iso_utc(now),
            })
            return {
                "session_id": active.session_id,
                "status": "paused",
                "reason": reason,
                "paused_at": crypto.iso_utc(now),
            }

    async def resume_session(self) -> Dict[str, Any]:
        async with self._lock:
            active = self._require_active()
            if self.state != PAUSED or active.pause is None:
                raise SessionError("Recording is not paused.")

            paused_for = time.monotonic() - active.pause.started_monotonic
            active.paused_seconds += paused_for
            now = datetime.now(timezone.utc)

            entry = active.spool.append_chain_entry("resume", crypto.resume_payload(
                at=now, paused_seconds=paused_for))

            active.pauses.append({
                "reason": active.pause.reason,
                "authorised_by": active.pause.authorised_by,
                "from": crypto.iso_utc(active.pause.started_at),
                "to": crypto.iso_utc(now),
                "seconds": round(paused_for, 1),
            })
            active.pause = None

            # Segmenter keeps running across a pause; only capture restarts. Its
            # segment clock is moved to now, otherwise the next segment's
            # timestamps would continue from before the pause and imply audio that
            # was never recorded.
            active.segmenter.set_segment_start(now)
            try:
                active.recorder.start()
            except AudioCaptureError as exc:
                self.state = PAUSED
                raise SessionError(
                    "The microphone could not be reopened. Check the device and try again."
                ) from exc

            self.state = RECORDING
            self._spawn(self._uploader.notify_resume(active.spool, entry))

            logger.info("Session %s resumed after %.1f s", active.session_id, paused_for)
            self._emit("recording_resumed", {
                "session_id": active.session_id,
                "paused_seconds": round(paused_for, 1),
                "resumed_at": crypto.iso_utc(now),
            })
            return {
                "session_id": active.session_id,
                "status": "recording",
                "paused_seconds": round(paused_for, 1),
            }

    # ---- stopping ----

    async def stop_session(self, *, reason: str = "doctor_stopped") -> Dict[str, Any]:
        async with self._lock:
            if self._active is None:
                return {"status": "not_recording", "session_id": None}
            return await self._close_active(reason=reason)

    async def _close_active(self, *, reason: str,
                            at: Optional[datetime] = None) -> Dict[str, Any]:
        active = self._active
        assert active is not None
        self.state = CLOSING

        # If we are closing from a paused state, account for the open pause.
        if active.pause is not None:
            active.paused_seconds += time.monotonic() - active.pause.started_monotonic
            active.pauses.append({
                "reason": active.pause.reason,
                "authorised_by": active.pause.authorised_by,
                "from": crypto.iso_utc(active.pause.started_at),
                "to": crypto.iso_utc(datetime.now(timezone.utc)),
                "seconds": round(time.monotonic() - active.pause.started_monotonic, 1),
                "resumed": False,
            })
            active.pause = None

        if active.recorder.is_running:
            stats = active.recorder.stop()
            active.audio_seconds += stats.bytes_captured / max(1, active.recorder.bytes_per_second)
            self._check_capture_health(active, stats)

        # Seals the tail as the final segment.
        active.segmenter.stop(seal_remaining=True)

        close_entry = active.spool.close(
            duration_seconds=active.audio_seconds,
            paused_seconds=active.paused_seconds,
            reason=reason,
            at=at,
        )
        verdict = active.spool.verify_chain()
        if not verdict.ok:
            logger.critical("Local chain verification failed for %s: %s",
                            active.session_id, verdict.reason)
            self._emit("integrity_alert", {
                "session_id": active.session_id,
                "alert_type": "local_chain_invalid",
                "detail": verdict.reason,
            })

        # Also fire-and-forget: the drain loop retries close for any session that
        # was closed while the backend was unreachable, so stopping a recording
        # stays instant regardless of the network.
        self._spawn(self._uploader.close_remote(
            active.spool,
            duration_seconds=active.audio_seconds,
            paused_seconds=active.paused_seconds,
        ))
        self._uploader.nudge()

        result = {
            "status": "stopped",
            "session_id": active.session_id,
            "duration_seconds": round(active.audio_seconds, 1),
            "paused_seconds": round(active.paused_seconds, 1),
            "segment_count": len(active.spool.segments),
            "pauses": active.pauses,
            "reason": reason,
            "chain_ok": verdict.ok,
        }

        logger.info("Session %s stopped: %.1f s audio, %s segments, %s pause(s), reason=%s",
                    active.session_id, active.audio_seconds,
                    len(active.spool.segments), len(active.pauses), reason)

        self._active = None
        self.state = IDLE
        self._emit("recording_stopped", result)
        return result

    async def force_reset(self, *, actor: str = "unknown", reason: str = "") -> Dict[str, Any]:
        """
        Clear a stuck state without discarding audio.

        v1's force reset dropped the session and its recording. Here everything
        already sealed stays in the spool and continues uploading; only the live
        state machine is reset, and the event is recorded.
        """
        async with self._lock:
            previous = self._active.session_id if self._active else None
            logger.warning("Force reset requested by %s (reason=%s), session=%s",
                           actor, reason or "-", previous or "-")

            if self._active is not None:
                try:
                    await self._close_active(reason=f"force_reset:{actor}")
                except Exception as exc:
                    logger.error("Force reset could not close cleanly: %s", exc)
                    self._active = None
                    self.state = IDLE

            self._emit("integrity_alert", {
                "session_id": previous,
                "alert_type": "force_reset",
                "detail": f"actor={actor} reason={reason}",
            })
            return {
                "status": "reset_complete",
                "previous_session_id": previous,
                "audio_preserved": True,
            }

    # ---- callbacks ----

    def _on_segment_sealed(self, sealed: SealedSegment) -> None:
        """
        Runs on the segmenter thread. Writes to the spool, then wakes the uploader.

        Disk I/O here is deliberate: this thread exists so the capture thread does
        not have to do it.
        """
        active = self._active
        if active is None:
            logger.error("Sealed segment arrived with no active session; discarding is not an "
                         "option, writing to the last known spool is not safe - dropping")
            return

        segment = active.spool.seal_segment(
            sealed.pcm,
            captured_start_at=sealed.captured_start_at,
            captured_end_at=sealed.captured_end_at,
            rms_mean=sealed.rms_mean,
            is_final=sealed.is_final,
        )

        # A run of segments at the noise floor usually means a muted, unplugged or
        # physically covered microphone. Raised once per session, and only after
        # two consecutive silent segments: a single quiet clip is unremarkable, and
        # an alert per segment is noise nobody reads.
        if sealed.rms_mean < (self.cfg.segment.silence_rms / 4):
            active.consecutive_silent += 1
            if active.consecutive_silent >= 2 and not active.silence_alerted:
                active.silence_alerted = True
                self._emit("integrity_alert", {
                    "session_id": active.session_id,
                    "seq_no": segment.seq_no,
                    "alert_type": "silent_session",
                    "detail": (f"{active.consecutive_silent} consecutive segments at the "
                               f"noise floor (mean RMS {sealed.rms_mean:.1f}) - check that "
                               f"the microphone is connected and not muted"),
                })
        else:
            active.consecutive_silent = 0

        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._uploader.nudge)

        self._emit("segment_sealed", {
            "session_id": active.session_id,
            "seq_no": segment.seq_no,
            "duration_seconds": round(segment.duration_seconds, 1),
            "is_final": segment.is_final,
        })

    def _on_capture_error(self, message: str) -> None:
        active = self._active
        logger.critical("Capture failure: %s", message)
        self._emit("integrity_alert", {
            "session_id": active.session_id if active else None,
            "alert_type": "capture_failed",
            "detail": message,
        })

    def _check_capture_health(self, active: ActiveSession, stats) -> None:
        if stats.overruns:
            self._emit("integrity_alert", {
                "session_id": active.session_id,
                "alert_type": "capture_overrun",
                "detail": f"{stats.overruns} chunk(s) dropped because the segmenter fell behind",
            })
        # A few dropped reads happen when a headset is unplugged or Windows
        # switches default device, and cost a fraction of a second. Reporting
        # every one put "20 read error(s) from the input device" in front of a
        # doctor who could do nothing with it. Only say something when enough
        # audio is missing to matter.
        lost_seconds = stats.read_errors * self.cfg.audio.frames_per_buffer / max(
            1, self.cfg.audio.sample_rate)
        if lost_seconds >= 1.0:
            self._emit("integrity_alert", {
                "session_id": active.session_id,
                "alert_type": "capture_read_errors",
                "detail": (f"about {lost_seconds:.0f}s of audio was lost - check the "
                           f"microphone is firmly connected"),
            })
        elif stats.read_errors:
            logger.info("%s transient read error(s), about %.2fs lost",
                        stats.read_errors, lost_seconds)

    # ---- heartbeat ----

    async def _heartbeat_loop(self) -> None:
        """
        Tell the server we are alive, and how deep the spool is.

        A missing heartbeat is how a killed agent or a stalled upload queue becomes
        visible centrally instead of being discovered weeks later.
        """
        interval = max(10, self.cfg.ops.heartbeat_seconds)
        while True:
            try:
                await asyncio.sleep(interval)
                await self._uploader.heartbeat(self.heartbeat_payload())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Heartbeat failed: %s", exc)

    def heartbeat_payload(self) -> Dict[str, Any]:
        upload = self._uploader.status()
        active = self._active
        return {
            "device_id": self.device_id,
            "app_version": self.cfg.app_version,
            "protocol_version": self.cfg.protocol_version,
            "state": self.state,
            "session_id": active.session_id if active else None,
            "spool_bytes": upload["spool_bytes"],
            "spool_pressure": upload["spool_pressure"],
            "pending_segments": upload["pending_segments"],
            "sent_at": crypto.iso_utc(datetime.now(timezone.utc)),
        }

    # ---- status ----

    def status(self) -> Dict[str, Any]:
        active = self._active
        upload = self._uploader.status()

        payload: Dict[str, Any] = {
            "state": self.state,
            "is_recording": self.state == RECORDING,
            "is_paused": self.state == PAUSED,
            "session_id": active.session_id if active else None,
            "patient_ref": active.grant.patient_ref if active else None,
            "patient_name": active.patient_name if active else None,
            # Reported even with no session running, so the dashboard can
            # show who this machine is enrolled to before recording starts.
            "doctor_id": (active.doctor_id if active else None) or None,
            "hospital_id": self.hospital_id or None,
            "started_at": crypto.iso_utc(active.opened_at) if active else None,
            "segment_count": len(active.spool.segments) if active else 0,
            "duration_seconds": round(self._live_duration(active), 1) if active else 0.0,
            "paused_seconds": round(active.paused_seconds, 1) if active else 0.0,
            "upload": upload,
            "spool_capacity_hours": round(self.cfg.spool_seconds() / 3600, 1),
        }
        if active and active.pause:
            payload["pause"] = {
                "reason": active.pause.reason,
                "reason_detail": active.pause.reason_detail,
                "authorised_by": active.pause.authorised_by,
                "since": crypto.iso_utc(active.pause.started_at),
                "seconds": round(time.monotonic() - active.pause.started_monotonic, 1),
            }
        return payload

    @staticmethod
    def _live_duration(active: Optional[ActiveSession]) -> float:
        if active is None:
            return 0.0
        live = active.recorder.duration_seconds if active.recorder.is_running else 0.0
        return active.audio_seconds + live

    # ---- helpers ----

    def _spawn(self, coro) -> None:
        """
        Run a backend call without blocking the control path.

        Pause, resume and close must never wait on the network. The chain entry is
        already durably journaled in the spool, and the full chain is delivered
        again inside the manifest at close, so a failed notification costs nothing
        but a little latency in the operator dashboard. Awaiting these calls made a
        backend outage freeze pause and stop for the length of the retry schedule.
        """
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._on_background_done)

    def _on_background_done(self, task: "asyncio.Task") -> None:
        """
        Discard the task and surface anything it raised.

        Discarding without reading the exception loses it silently, which is how a
        failed close looked identical to a successful one.
        """
        self._background.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Background backend call failed: %s", exc, exc_info=exc)

    def _require_active(self) -> ActiveSession:
        if self._active is None:
            raise SessionError("No recording is in progress.")
        return self._active

    def _pseudonym(self, value: str) -> str:
        if not self.cfg.ops.redact_logs:
            return value
        return crypto.pseudonymise(value, self._log_salt)

    def _emit(self, event: str, data: Dict[str, Any]) -> None:
        if event == "integrity_alert":
            self.last_alert = f"{data.get('alert_type')}: {data.get('detail', '')}"
        if self._on_event:
            try:
                self._on_event(event, data)
            except Exception as exc:
                logger.debug("Event handler for %s raised: %s", event, exc)


__all__ = ["SessionController", "SessionError", "IDLE", "RECORDING", "PAUSED", "CLOSING"]
