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

* The clinic comes from the device enrolment, always (SRS-INV-01). The doctor
  and patient come from a grant the AIMS LAB server issued for CMED's trigger.
* A new trigger does not end the current consultation until CMED has said its
  prescription is built (the gate, SRS §7.7).
* Pause requires a reason from a fixed list, and a supervisor's name once the
  expected duration passes the configured threshold.
* Stopping never deletes audio, with one exception: "Patient did not consent"
  deletes the consultation everywhere (SRS 3.2 §7.8a). Otherwise local files go
  only when a signed receipt proves the server holds a verified copy.
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

# The Stop reason that erases a consultation (SRS 3.2 §7.8a).
REFUSAL_REASON = "patient_did_not_consent"

# Recorded as a pause the moment Stop is pressed, so the microphone cuts at once
# and the gap is explained in the chain while the doctor picks a reason
# (SRS-UIX-08). Not a reason anyone chooses; the on-screen control sets it.
STOP_HOLD_REASON = "stop_requested"

# Hard refusals from the server, and what the doctor is told (SRS-GRT-08).
REFUSAL_MESSAGES = {
    "CLINIC_MISMATCH": "This PC is registered to a different clinic, so the recording "
                       "was stopped and deleted. AIMS LAB has been alerted.",
    "DEVICE_NOT_ENROLLED": "This PC is not registered with AIMS LAB, so the recording "
                           "was stopped and deleted.",
    "AUTHORISATION_FAILED": "This recording could not be authorised, so it was stopped "
                            "and deleted.",
    "DOCTOR_NOT_AT_CLINIC": "This doctor is not registered at this clinic.",
    "MISSING_FIELD": "CMED's message was incomplete, so the recording was stopped.",
    "INVALID_IDENTIFIER": "CMED's message was not valid, so the recording was stopped.",
}


class SessionError(RuntimeError):
    """
    A session request that must be refused, with a reason safe to show a user.

    `code` is the Channel A reply code the page receives (api/protocol.py).
    """

    def __init__(self, message: str, *, code: str = "REFUSED"):
        super().__init__(message)
        self.code = code


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
    patient_ref: str
    patient_name: str
    recorder: AudioRecorder
    segmenter: Segmenter
    opened_at: datetime
    # Who is consulting, from CMED's trigger. A shared room rotates doctors.
    doctor_id: str = ""
    # API 1's start_time, exactly as CMED's server wrote it. Together with the
    # patient it identifies the consultation, so a repeated trigger for the same
    # one is recognised rather than refused by the gate.
    trigger_start_time: str = ""
    # Set by prescription_built (API 3). Until then a new trigger is refused and
    # this recording carries on (SRS-GAT-01..04).
    armed: bool = False
    # SRS 3.2 §5: recording starts before the server has answered.
    authorisation: str = "pending"        # pending | granted
    confirmation: str = ""                # confirming | confirmed | unconfirmed
    # Stop was pressed and the reason form is open (SRS-UIX-08).
    stop_hold: bool = False
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
        trigger,
        *,
        grant: Optional[Grant] = None,
        patient_name: str = "",
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        API 1: CMED's trigger. The microphone opens now (SRS-GRT-07).

        Permission is asked from the AIMS LAB server alongside capture, and this
        returns once capture is running and either the server has answered or a
        short wait has passed:

            authorisation "granted"   the page is told 200 RECORDING_STARTED
            authorisation "pending"   202 RECORDING_PROVISIONAL; still asking

        A hard refusal inside the wait raises; one after it stops the recording
        and deletes what was captured (SRS-GRT-08). A server that cannot be
        reached never stops a recording - nothing is uploaded until it answers.

        `grant` is passed only in development mode, where no server is asked.
        """
        auth_task = None
        async with self._lock:
            previous_id: Optional[str] = None

            # One instant for the whole handover. CMED treats consultations as
            # contiguous - the second patient's start time is the first
            # patient's end time - so both must be stamped from the same value.
            boundary = datetime.now(timezone.utc)

            if not self.device_id:
                raise SessionError(
                    "This PC is not enrolled with the AIMS LAB server. Contact IT - "
                    "recordings cannot be attributed or archived until it is.",
                    code="DEVICE_NOT_ENROLLED")

            # Who is consulting comes from CMED, every time, with no fallback: a
            # room runs two shifts, and falling back to a machine's doctor filed
            # afternoon consultations under the morning shift.
            doctor_id = trigger.doctor_id
            patient_ref = trigger.patient_id
            if not doctor_id:
                raise SessionError(
                    "CMED did not say which doctor is seeing this patient, so the "
                    "recording cannot be attributed.", code="MISSING_FIELD")

            # The clinic is the machine's, always (SRS-INV-01; decision D1). The
            # server refuses a grant for another clinic; this is the same rule
            # for a grant supplied directly.
            hospital_id = self.hospital_id
            if grant is not None and grant.hospital_id and grant.hospital_id != hospital_id:
                self._emit("integrity_alert", {
                    "session_id": None,
                    "alert_type": "clinic_mismatch",
                    "detail": (f"device enrolled at {hospital_id}, "
                               f"grant is for {grant.hospital_id}"),
                })
                raise SessionError(
                    "This PC is registered to a different clinic, so the recording "
                    "cannot start. AIMS LAB has been alerted.", code="CLINIC_MISMATCH")

            if self._active is not None:
                active = self._active
                # The same consultation triggered twice - a double click or a
                # page reload - is not a new patient.
                if (active.trigger_start_time == trigger.start_time
                        and active.patient_ref == patient_ref):
                    raise SessionError("This consultation is already being recorded.",
                                       code="SESSION_ALREADY_ACTIVE")

                # The gate (SRS-GAT-03). Until the open consultation's
                # prescription is built, a new trigger is a doctor glancing at
                # another patient, not the next consultation.
                if not active.armed:
                    logger.info("Trigger for patient %s refused: session %s is not armed",
                                self._pseudonym(patient_ref), active.session_id)
                    self._emit("trigger_refused", {
                        "session_id": active.session_id,
                        "patient_ref": patient_ref,
                        "doctor_id": doctor_id,
                        "at": crypto.iso_utc(boundary),
                        "reason": "gate_not_armed",
                    })
                    raise SessionError(
                        "The current consultation has not been completed yet.",
                        code="GATE_NOT_ARMED")

                previous_id = active.session_id
                logger.info("Closing session %s before opening a new one", previous_id)
                await self._close_active(reason="superseded_by_new_patient", at=boundary)

            if not self._spool.has_capacity(self.cfg.audio.bytes_per_second * 240):
                raise SessionError(
                    "Local audio buffer is full. Recording cannot start until the "
                    "backlog uploads. Contact support.", code="AGENT_NOT_READY")

            spool_session = self._spool.open_session(
                device_key=self._device_key,
                device_id=self.device_id,
                doctor_id=doctor_id,
                hospital_id=hospital_id,
                patient_ref=patient_ref,
                # Consent is taken at reception (SRS 3.2 §7.8a). The field stays
                # in the first chain entry so the chain format is unchanged.
                consent_method="reception",
                audio={
                    "sample_rate": self.cfg.audio.sample_rate,
                    "channels": self.cfg.audio.channels,
                    "sample_width": self.cfg.audio.sample_width,
                },
                session_id=session_id,
                # With a grant in hand there is nothing to ask for later.
                trigger=None if grant is not None else trigger.fields(),
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
                    "not in use by another application.", code="AGENT_NOT_READY") from exc

            spool_session.live = True
            active = ActiveSession(
                spool=spool_session,
                patient_ref=patient_ref,
                patient_name=patient_name,
                recorder=recorder,
                segmenter=segmenter,
                opened_at=opened_at,
                doctor_id=doctor_id,
                trigger_start_time=trigger.start_time,
                authorisation="granted" if grant is not None else "pending",
                confirmation="" if grant is not None else "confirming",
            )
            self._active = active
            self.state = RECORDING

            await self._uploader.track(spool_session)
            self._uploader.nudge()

            logger.info("Session %s opened for patient %s by doctor %s at %s",
                        spool_session.session_id, self._pseudonym(patient_ref),
                        self._pseudonym(doctor_id), hospital_id)
            self._emit("recording_started", {
                "session_id": spool_session.session_id,
                "patient_ref": patient_ref,
                "doctor_id": doctor_id,
                "hospital_id": hospital_id,
            })

            if grant is None:
                auth_task = asyncio.create_task(self._authorise_live(active))
                self._background.add(auth_task)
                auth_task.add_done_callback(self._on_background_done)

        # Outside the lock: a refusal has to take it to stop the recording.
        if auth_task is not None:
            try:
                outcome = await asyncio.wait_for(asyncio.shield(auth_task),
                                                 timeout=self._knob("authorise_wait_seconds", 1.5))
            except asyncio.TimeoutError:
                outcome = None
            if outcome is not None and outcome.status == "refused":
                raise SessionError(
                    outcome.message or REFUSAL_MESSAGES.get(
                        outcome.code, "This recording could not be authorised."),
                    code=outcome.code if outcome.code in REFUSAL_MESSAGES
                    else "AUTHORISATION_FAILED")

        return {
            "session_id": active.session_id,
            "status": "recording",
            "started_at": crypto.iso_utc(active.opened_at),
            "previous_session_stopped": previous_id is not None,
            "previous_session_id": previous_id,
            "armed": False,
            "authorisation": active.authorisation,
            "confirmation": active.confirmation,
        }

    # ---- authorisation and confirmation (SRS 3.2 §5) ----

    async def _authorise_live(self, active: ActiveSession):
        """
        Ask for the live consultation's grant, and keep asking while it records.

        Once the consultation closes, the upload loop takes over the asking - so
        an outage, a restart or a whole offline morning still ends with every
        recording either authorised and uploaded, or refused and deleted.
        """
        while True:
            outcome = await self._uploader.authorise(active.spool)
            if outcome.status == "granted":
                active.authorisation = "granted"
                confirmed = outcome.confirmation == "confirmed"
                active.confirmation = "confirmed" if confirmed else "confirming"
                self._emit("session_authorised", {"session_id": active.session_id})
                self._emit("session_confirmed" if confirmed else "session_confirming",
                           {"session_id": active.session_id})
                if not confirmed:
                    self._spawn(self._watch_confirmation(active))
                return outcome
            if outcome.status == "refused":
                if outcome.code != "PATIENT_REFUSED":
                    await self._abandon(active, code=outcome.code)
                return outcome
            if self._active is not active:
                return outcome
            await asyncio.sleep(self._knob("authorise_retry_seconds", 10.0))

    async def _abandon(self, active: ActiveSession, *, code: str) -> None:
        """
        The server refused this consultation (SRS-GRT-08): stop capture and
        delete what was captured. Nothing of it was ever uploaded - the upload
        loop sends nothing before a grant.
        """
        async with self._lock:
            if self._active is active:
                if active.recorder.is_running:
                    active.recorder.stop()
                active.segmenter.stop(seal_remaining=False)
                self._active = None
                self.state = IDLE
            active.spool.live = False
        await self._uploader.forget(active.spool)
        self._spool.remove(active.spool, reason=f"authorisation refused ({code})")
        logger.warning("Session %s refused by the server (%s); audio deleted",
                       active.session_id, code)
        self._emit("integrity_alert", {
            "session_id": active.session_id,
            "alert_type": "authorisation_refused",
            "detail": code,
        })
        self._emit("recording_stopped", {
            "session_id": active.session_id,
            "status": "stopped",
            "reason": "authorisation_refused",
            "code": code,
        })

    async def _watch_confirmation(self, active: ActiveSession) -> None:
        """
        SRS-CNF-07 and -08: ask every five seconds whether CMED's API 2 has
        confirmed this recording. After two minutes it is shown as unconfirmed.
        The recording is never cut for this.
        """
        deadline = time.monotonic() + self._knob("confirm_deadline_seconds", 120.0)
        while active.confirmation == "confirming" and self._active is active:
            await asyncio.sleep(self._knob("confirm_poll_seconds", 5.0))
            state = await self._uploader.check_confirmation(active.spool)
            if state == "confirmed":
                active.confirmation = "confirmed"
                self._emit("session_confirmed", {"session_id": active.session_id})
                return
            if state == "unconfirmed" or time.monotonic() >= deadline:
                active.confirmation = "unconfirmed"
                self._emit("session_unconfirmed", {"session_id": active.session_id})
                return

    def _knob(self, name: str, default: float) -> float:
        security = getattr(self.cfg, "security", None)
        return float(getattr(security, name, default)) if security is not None else default

    # ---- the gate ----

    async def arm(self, *, patient_id: str, session_id: str) -> Dict[str, Any]:
        """
        API 3, part one: the prescription is built.

        Arms the gate so that the *next* trigger may end this consultation. The
        recording itself is not touched (SRS-IF1-12): the doctor usually counsels
        the patient for another minute or two, and that is worth keeping.
        """
        async with self._lock:
            active = self._active
            if active is None:
                raise SessionError("Nothing is being recorded.", code="NO_ACTIVE_SESSION")
            if active.patient_ref != patient_id or active.session_id != session_id:
                self._emit("integrity_alert", {
                    "session_id": active.session_id,
                    "alert_type": "arm_mismatch",
                    "detail": "prescription_built named a different patient or session",
                })
                raise SessionError("That signal belongs to a different consultation.",
                                   code="PATIENT_MISMATCH")
            if active.armed:
                return {"session_id": active.session_id, "armed": True, "already": True}

            active.armed = True
            logger.info("Session %s armed: the next patient will close it", active.session_id)
            self._emit("gate_armed", {"session_id": active.session_id})
            return {"session_id": active.session_id, "armed": True, "already": False}

    # ---- pause / resume ----

    async def pause_session(
        self,
        *,
        reason: str,
        reason_detail: str = "",
        authorised_by: str = "",
        expected_seconds: int = 0,
        internal: bool = False,
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

            if reason not in self.cfg.pause.reasons and not (
                    internal and reason == STOP_HOLD_REASON):
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

    async def hold_for_stop(self) -> Dict[str, Any]:
        """
        Stop was pressed on the on-screen control: cut the microphone now.

        The press cuts capture before any form is filled (SRS-UIX-08) - if the
        patient has just objected, they must not be recorded while the doctor
        chooses a reason. The gap is a pause in the chain, so it is explained.
        The form then closes the session, or Cancel releases the hold.
        """
        active = self._active
        if active is None:
            return {"status": "not_recording", "session_id": None}
        if self.state == PAUSED:
            # Already silent; there is nothing to hold and nothing to release.
            active.stop_hold = False
            return {"status": "paused", "session_id": active.session_id, "held": False}
        result = await self.pause_session(
            reason=STOP_HOLD_REASON,
            reason_detail="Stop pressed; waiting for the reason",
            internal=True)
        active.stop_hold = True
        return {**result, "held": True}

    async def release_stop_hold(self) -> Dict[str, Any]:
        """The doctor cancelled Stop: recording carries on (SRS-UIX-07)."""
        active = self._active
        if active is None or not active.stop_hold:
            return {"status": "unchanged",
                    "session_id": active.session_id if active else None}
        active.stop_hold = False
        return await self.resume_session()

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

    async def stop_session(self, *, reason: str = "doctor_stopped",
                           detail: str = "") -> Dict[str, Any]:
        if reason == REFUSAL_REASON:
            if self._active is None:
                return {"status": "not_recording", "session_id": None}
            return await self.refuse_session()
        async with self._lock:
            if self._active is None:
                return {"status": "not_recording", "session_id": None}
            return await self._close_active(reason=reason, detail=detail)

    async def _close_active(self, *, reason: str, at: Optional[datetime] = None,
                            detail: str = "") -> Dict[str, Any]:
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
            detail=detail,
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

        active.spool.live = False
        self._active = None
        self.state = IDLE
        self._emit("recording_stopped", result)
        return result

    async def refuse_session(self) -> Dict[str, Any]:
        """
        The patient did not consent (SRS 3.2 §7.8a).

        The microphone stops at once, every piece of this consultation is
        deleted from the PC, and the upload loop tells the server - which erases
        what it holds - until the server acknowledges (SRS-CNS-03..05).
        """
        async with self._lock:
            active = self._require_active()
            if active.recorder.is_running:
                active.recorder.stop()
            # Stop the cutter before deleting, so no piece lands afterwards.
            active.segmenter.stop(seal_remaining=False)
            deleted = active.spool.refuse()
            active.spool.live = False
            self._active = None
            self.state = IDLE

        self._uploader.nudge()
        logger.warning("Session %s refused by the patient; %s piece(s) deleted locally",
                       active.session_id, deleted)
        result = {"status": "refused", "session_id": active.session_id,
                  "reason": REFUSAL_REASON, "pieces_deleted": deleted}
        self._emit("session_refused", {"session_id": active.session_id})
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
            "oldest_pending_seconds": upload.get("oldest_pending_seconds", 0),
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
            "patient_ref": active.patient_ref if active else None,
            "patient_name": active.patient_name if active else None,
            "armed": active.armed if active else False,
            "authorisation": active.authorisation if active else None,
            "confirmation": active.confirmation if active else None,
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
            raise SessionError("No recording is in progress.", code="NO_ACTIVE_SESSION")
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


__all__ = ["SessionController", "SessionError", "IDLE", "RECORDING", "PAUSED", "CLOSING",
           "REFUSAL_REASON", "REFUSAL_MESSAGES", "STOP_HOLD_REASON"]
