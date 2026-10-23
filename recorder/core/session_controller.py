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
  prescription is built (the gate, SRS §7.7). Until then every patient opened
  is accepted and written into the recording as a signed marker, with the
  second of audio it happened at; the AIMS LAB server decides afterwards whose
  consultation each part was. A recording also ends by itself two minutes after
  its prescription, and after fifteen minutes with nothing from CMED.
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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from core import crypto
from core.crypto import DeviceKey, Grant
from core.disk import MIN_FREE_BYTES, DriveFacts
from core.microphone import make_ready
from core.power import keep_awake
from core.recorder import AudioCaptureError, AudioRecorder
from core.simple_splitter import SealedSegment, Segmenter
from core.spool import SessionSpool, Spool
from core.start_keeper import StartKeeper
from core.uploader import UploadManager

logger = logging.getLogger(__name__)

IDLE = "idle"
RECORDING = "recording"
PAUSED = "paused"
CLOSING = "closing"

# The Stop reason that erases a consultation (SRS 3.2 §7.8a).
REFUSAL_REASON = "patient_did_not_consent"
# How often an interrupted recording that could not be closed at startup
# (the disk was full) is tried again.
RECOVERY_RETRY_SECONDS = 30.0

# Recorded as a pause the moment Stop is pressed, so the microphone cuts at once
# and the gap is explained in the chain while the doctor picks a reason
# (SRS-UIX-08). Not a reason anyone chooses; the on-screen control sets it.
STOP_HOLD_REASON = "stop_requested"
# Windows asked whether it may shut down; the answer is not in yet.
SHUTDOWN_HOLD_REASON = "pc_shutdown_pending"
_INTERNAL_PAUSES = (STOP_HOLD_REASON, SHUTDOWN_HOLD_REASON)

# What the doctor's screen says when the server has not accepted a recording
# yet. The recording carries on and is kept on this PC; nothing is deleted.
HOLD_MESSAGES = {
    "CLINIC_MISMATCH": "Recording. The AIMS LAB server does not recognise this clinic "
                       "yet, so the recording is kept on this PC. AIMS LAB has been alerted.",
    "DEVICE_NOT_ENROLLED": "Recording. The AIMS LAB server does not recognise this PC "
                           "yet, so the recording is kept on this PC.",
    "AUTHORISATION_FAILED": "Recording. The AIMS LAB server has not accepted this "
                            "recording yet, so it is kept on this PC.",
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
    # Set by prescription_built (API 3). Until then a new trigger does not end
    # this recording (SRS-GAT-01..04): it is written in as a marker, and the
    # server decides from the markers whose consultation each part was.
    armed: bool = False
    # The patient CMED has open now (the latest click or prescription), and
    # every patient opened during this recording: patient_id -> the five
    # fields as CMED first sent them.
    focus: Dict[str, str] = field(default_factory=dict)
    seen: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # Monotonic times: the first prescription (the counselling limit counts
    # from it) and the last thing CMED said about this recording (the
    # no-activity limit counts from that).
    armed_at: Optional[float] = None
    last_cmed_at: float = field(default_factory=time.monotonic)
    # Ends the recording at either limit. Cancelled when it closes.
    limit_task: Optional[asyncio.Task] = None
    # Asks the server for the grant while this consultation records. Cancelled
    # when it closes: from then on the upload loop does the asking.
    auth_task: Optional[asyncio.Task] = None
    # SRS 3.2 §5: recording starts before the server has answered.
    authorisation: str = "pending"        # pending | granted
    confirmation: str = ""                # confirming | confirmed | unconfirmed
    # Stop was pressed and the reason form is open (SRS-UIX-08).
    stop_hold: bool = False
    # Paused because Windows asked to shut down; resumed if it is cancelled.
    shutdown_hold: bool = False
    audio_seconds: float = 0.0
    paused_seconds: float = 0.0
    pause: Optional[PauseRecord] = None
    pauses: List[Dict[str, Any]] = field(default_factory=list)
    # Where this recording's clips are written, bound when it opens. A clip
    # that waited for disk space is written into its own recording, never into
    # whichever one is open when the disk frees up.
    sink: Optional["SegmentSink"] = None

    @property
    def session_id(self) -> str:
        return self.spool.session_id

    @property
    def consecutive_silent(self) -> int:
        return self.sink.consecutive_silent if self.sink else 0


@dataclass
class SegmentSink:
    """One recording's clip writer: its spool and its own silence count."""
    spool: SessionSpool
    consecutive_silent: int = 0
    silence_alerted: bool = False


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

        self._state = IDLE
        self._drive = DriveFacts(getattr(spool, "root", "."))
        self._disk_full_reported = False
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
        # The consultation CMED last asked for, until it ends normally: in
        # memory, and on disk so a restart can pick it up (see start_keeper).
        paths = getattr(cfg, "paths", None)
        self._keeper = StartKeeper(getattr(paths, "state_dir", None))
        self._wanted: Optional[Dict[str, str]] = None
        self._start_retry: Optional[asyncio.Task] = None

    # ---- startup / shutdown ----

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._drive.probe_in_background()
        await self._recover_previous_sessions()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="Heartbeat")
        self._spawn(self._resume_after_restart())

    async def close(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        if self._start_retry is not None and not self._start_retry.done():
            self._start_retry.cancel()
        if self._active:
            await self.stop_session(reason="agent_shutdown")
        # Nothing may outlive the controller: a task destroyed while still
        # pending is finalised at some arbitrary later moment instead.
        pending = [task for task in self._background if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _recover_previous_sessions(self) -> None:
        """
        Adopt anything left in the spool by a previous run.

        A session that was still open when the process died is closed short and the
        interruption is recorded, so the gap is explained rather than mysterious.
        """
        try:
            found = list(self._spool.recover(self._device_key))
        except Exception as exc:
            logger.critical("The spool could not be read at startup: %s", exc)
            self._emit("integrity_alert", {"session_id": None, "alert_type": "spool_unreadable",
                                           "detail": str(exc)})
            return
        retry = []
        for session in found:
            try:
                await self._recover_one(session)
            except OSError as exc:
                # A full disk must not keep the recorder from starting: the
                # next patient can still be recorded once there is room. This
                # one is closed later, when the disk has room.
                self._report_disk_full(session.session_id,
                                       f"an interrupted recording could not be closed ({exc})")
                retry.append(session)
            except Exception as exc:
                logger.exception("Could not recover session %s", session.session_id)
                self._emit("integrity_alert", {"session_id": session.session_id,
                                               "alert_type": "recovery_failed",
                                               "detail": str(exc)})
        if retry:
            self._spawn(self._recover_later(retry))

    async def _recover_later(self, sessions) -> None:
        while sessions:
            await asyncio.sleep(RECOVERY_RETRY_SECONDS)
            still = []
            for session in sessions:
                try:
                    await self._recover_one(session)
                except OSError:
                    still.append(session)
            sessions = still

    async def _recover_one(self, session) -> None:
        """
        One session left in the spool by a previous run. One that was still
        open when the process died is closed short and the interruption is
        recorded, so the gap is explained rather than mysterious.
        """
        if session.closed_at is None:
            logger.warning("Session %s was interrupted; closing it short",
                           session.session_id)
            saved = self._seal_partial(session)
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
                "detail": ("session recovered from the spool after an unclean shutdown"
                           + (f"; {saved:.1f}s of the clip in progress were saved from disk"
                              if saved else "")),
            })
        session.clear_partial()
        await self._uploader.track(session)

    @staticmethod
    def _seal_partial(session) -> float:
        """
        The clip that was being assembled when the process stopped, kept on disk
        every few seconds, sealed into the chain as the recording's last clip.
        Returns the seconds saved.
        """
        try:
            found = session.read_partial()
        except Exception as exc:
            logger.error("Clip in progress of %s unreadable: %s", session.session_id, exc)
            return 0.0
        if not found:
            return 0.0
        started, pcm = found
        audio = session.audio
        seconds = len(pcm) / max(1, audio["sample_rate"] * audio["channels"]
                                 * audio["sample_width"])
        from core.simple_splitter import _rms
        session.seal_segment(pcm, captured_start_at=started,
                             captured_end_at=started + timedelta(seconds=seconds),
                             rms_mean=_rms(pcm, audio["sample_width"]), is_final=True)
        logger.warning("Saved %.1fs of %s that was not yet sealed when it stopped",
                       seconds, session.session_id)
        return seconds

    # ---- opening ----

    async def open_session(
        self,
        trigger,
        *,
        grant: Optional[Grant] = None,
        patient_name: str = "",
        session_id: Optional[str] = None,
        retrying: bool = False,
        continues_session_id: Optional[str] = None,
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
                # The gate (SRS-GAT-03). Until the open consultation's
                # prescription is built, a patient opened in CMED does not end
                # it: it may be a glance at another file, the doctor correcting
                # a wrong profile picked at the start, or - after a follow-up
                # that needs no prescription - the next patient. Which one is
                # not knowable at the click, so the click is accepted (CMED's
                # page carries on exactly as for a start, and sends that
                # patient's API 2), the recording carries on, and the click is
                # written in as a signed marker. The server decides afterwards.
                #
                # This used to refuse with GATE_NOT_ARMED. CMED's page then
                # stopped for that patient: no API 2, an error in front of the
                # doctor, and a follow-up that could never be finished.
                if not active.armed:
                    self._note_click(active, trigger, boundary)
                    return {
                        "session_id": active.session_id,
                        "status": "recording",
                        "continued": True,
                        "started_at": crypto.iso_utc(active.opened_at),
                        "previous_session_stopped": False,
                        "previous_session_id": None,
                        "armed": False,
                        "authorisation": active.authorisation,
                        "confirmation": active.confirmation,
                    }

                # Armed: the patient already open again - a double click, a
                # reload - is the consultation still running.
                if active.focus.get("patient_id") == patient_ref:
                    raise SessionError("This consultation is already being recorded.",
                                       code="SESSION_ALREADY_ACTIVE")

                previous_id = active.session_id
                logger.info("Closing session %s before opening a new one", previous_id)
                await self._close_active(reason="superseded_by_new_patient", at=boundary)

            if not self._spool.has_capacity(self.cfg.audio.bytes_per_second * 240):
                raise SessionError(
                    "Local audio buffer is full. Recording cannot start until the "
                    "backlog uploads. Contact support.", code="AGENT_NOT_READY")
            # The drive itself, not only the spool's own cap: a C: drive full of
            # other files cannot hold the next clip however empty the spool is.
            free = self._drive.free_bytes()
            if free is not None and free < MIN_FREE_BYTES:
                self._report_disk_full(None, f"only {free / 2**20:.0f} MB free on the PC's "
                                             f"disk; a new recording needs at least 1 GB")
                raise SessionError(
                    "This PC's disk is almost full, so a new recording cannot start. "
                    "Free some space on the C: drive.", code="AGENT_NOT_READY")

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
                continues_session_id=continues_session_id,
            )

            sink = SegmentSink(spool=spool_session)
            segmenter = Segmenter(
                sample_rate=self.cfg.audio.sample_rate,
                channels=self.cfg.audio.channels,
                sample_width=self.cfg.audio.sample_width,
                min_seconds=self.cfg.segment.min_seconds,
                max_seconds=self.cfg.segment.max_seconds,
                grace_seconds=self.cfg.segment.grace_seconds,
                silence_rms=self.cfg.segment.silence_rms,
                silence_hold_seconds=self.cfg.segment.silence_hold_seconds,
                on_segment=lambda sealed, sink=sink: self._seal_into(sink, sealed),
                on_partial=spool_session.write_partial,
                partial_every_seconds=getattr(self.cfg.segment, "partial_flush_seconds", 3.0),
                on_write_error=lambda exc, waiting, sid=spool_session.session_id:
                    self._report_disk_full(sid, f"a clip could not be written ({exc}); "
                                                f"{waiting:.0f} s of audio kept in memory "
                                                f"and tried again every few seconds"),
                on_write_recovered=self._disk_recovered,
            )
            recorder = AudioRecorder(
                sample_rate=self.cfg.audio.sample_rate,
                channels=self.cfg.audio.channels,
                sample_width=self.cfg.audio.sample_width,
                frames_per_buffer=self.cfg.audio.frames_per_buffer,
                input_device_index=self.cfg.audio.input_device_index,
                on_chunk=segmenter.submit,
                on_error=self._on_capture_error,
                on_device_change=self._on_microphone_switched,
                preferred_name=getattr(self.cfg.audio, "preferred_microphone", ""),
            )

            opened_at = boundary
            await self._ready_microphone()
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
                # Nothing was captured and nothing was sent, so there is no
                # consultation to keep. Left on disk, the directory came back at
                # the next start as an interrupted session - a false alert and an
                # empty upload - once for every retry of AGENT_NOT_READY.
                self._spool.remove(spool_session, reason="microphone unavailable at start")
                # Remembered and tried again by the recorder itself: the doctor
                # is already talking to the patient and must not have to click
                # the profile again.
                self._want(trigger.fields())
                if not retrying:
                    self._retry_start_later(trigger)
                raise SessionError(
                    "The microphone is unavailable. The recorder keeps trying and "
                    "starts by itself as soon as it can.", code="AGENT_NOT_READY") from exc

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
                focus=trigger.fields(),
                seen={patient_ref: trigger.fields()},
                sink=sink,
            )
            self._active = active
            self.state = RECORDING
            active.limit_task = self._background_task(self._watch_limits(active))
            self._want(trigger.fields(), session_id=spool_session.session_id)

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
                active.auth_task = auth_task
                self._background.add(auth_task)
                auth_task.add_done_callback(self._on_background_done)

        # Outside the lock: a refusal has to take it to stop the recording.
        if auth_task is not None:
            # The last request to the server failed: it will not answer inside
            # the wait, so CMED is told "recording" at once instead of after
            # 1.5 s of nothing. The grant is still asked for alongside.
            offline = getattr(self._uploader, "consecutive_failures", 0) > 0
            wait = 0.0 if offline else self._knob("authorise_wait_seconds", 1.5)
            try:
                outcome = await asyncio.wait_for(asyncio.shield(auth_task), timeout=wait)
            except asyncio.TimeoutError:
                outcome = None
            # A "no" here no longer stops anything: the recording carries on,
            # is kept on this PC, and is asked about again (see _authorise_live).
            if outcome is not None and outcome.status == "held":
                active.authorisation = "held"

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
        recording authorised and uploaded. A "no" from the server holds the
        recording on this PC and is asked again; it never deletes it.
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
                return outcome                  # the patient refused; handled by stop
            if self._active is not active:
                return outcome
            if outcome.status == "held":
                if active.authorisation != "held":
                    active.authorisation = "held"
                    self._emit("session_held", {
                        "session_id": active.session_id, "code": outcome.code,
                        "message": HOLD_MESSAGES.get(outcome.code,
                                                     HOLD_MESSAGES["AUTHORISATION_FAILED"]),
                    })
                await asyncio.sleep(self._knob("authorise_hold_retry_seconds", 300.0))
                continue
            await asyncio.sleep(self._knob("authorise_retry_seconds", 10.0))

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
        the patient for another minute or two, and that is worth keeping - up
        to the counselling limit.

        Built for any patient opened during this recording, it is written in as
        a marker: the strongest evidence the server has of who was consulted.
        Nothing is moved here. A wrong profile picked at the start is settled by
        the server from the markers, along with glances and follow-ups.
        """
        async with self._lock:
            active = self._active
            if active is None:
                raise SessionError("Nothing is being recorded.", code="NO_ACTIVE_SESSION")
            if active.session_id != session_id or patient_id not in active.seen:
                self._emit("integrity_alert", {
                    "session_id": active.session_id,
                    "alert_type": "arm_mismatch",
                    "detail": "prescription_built named a patient not opened during "
                              "this recording, or another session",
                })
                raise SessionError("That signal belongs to a different consultation.",
                                   code="PATIENT_MISMATCH")

            active.last_cmed_at = time.monotonic()
            patient = active.seen[patient_id]
            # A resend for the prescription already built changes nothing.
            resend = active.armed and active.focus.get("patient_id") == patient_id
            if not resend:
                self._mark(active, "prescription_built", patient,
                           datetime.now(timezone.utc))
                # A crash from here resumes the patient the doctor is on, armed.
                self._want(patient, session_id=active.session_id)
                if active.armed:
                    self._keeper.armed()     # want() above cleared it for a new patient
            if active.armed:
                return {"session_id": active.session_id, "armed": True, "already": True}

            active.armed = True
            active.armed_at = time.monotonic()
            self._keeper.armed()
            logger.info("Session %s armed: the next patient will close it", active.session_id)
            self._emit("gate_armed", {"session_id": active.session_id})
            return {"session_id": active.session_id, "armed": True, "already": False}

    def _note_click(self, active: ActiveSession, trigger, at: datetime) -> None:
        """
        A patient opened in CMED while this recording runs unarmed. Written in
        as a marker when the doctor moved to another patient - including back
        to one opened earlier, which is how a glance is recognised. A repeat of
        the patient already open changes nothing. Called with the lock held.
        """
        active.last_cmed_at = time.monotonic()
        fields = trigger.fields()
        first_time = trigger.patient_id not in active.seen
        active.seen.setdefault(trigger.patient_id, fields)
        if active.focus.get("patient_id") == trigger.patient_id:
            return
        self._mark(active, "patient_opened", active.seen[trigger.patient_id], at)
        self._want(active.seen[trigger.patient_id], session_id=active.session_id)
        logger.info("Patient %s opened during session %s%s", self._pseudonym(trigger.patient_id),
                    active.session_id, "" if first_time else " (again)")
        self._emit("patient_opened", {
            "session_id": active.session_id,
            "patient_ref": trigger.patient_id,
            "doctor_id": trigger.doctor_id,
            "at": crypto.iso_utc(at),
            "first_time": first_time,
        })

    def _mark(self, active: ActiveSession, event: str, patient: Dict[str, str],
              at: datetime):
        """Sign a marker into the chain at this second of audio; it is sent in order."""
        entry = active.spool.append_chain_entry("marker", crypto.marker_payload(
            event=event, patient=patient, audio_seconds=self._audio_position(active), at=at))
        active.focus = dict(patient)
        self._uploader.nudge()
        return entry

    def _audio_position(self, active: ActiveSession) -> float:
        """
        Where this recording's joined audio has reached, in seconds: every byte
        the segmenter has been given, sealed or not. Pauses capture nothing, so
        they add nothing. This is the offset the archive cuts at.
        """
        received = getattr(active.segmenter, "received_bytes", 0)
        return round(received / max(1, self.cfg.audio.bytes_per_second), 3)

    # ---- the limits ----

    async def _watch_limits(self, active: ActiveSession) -> None:
        """
        Time limits, for bench tests only: both are off (0) by default, because
        a clock must never end a real consultation (security.*_limit_seconds).
        When set, neither runs while paused: a paused recording is already
        silent, and the Stop form may be open with a reason that matters.
        """
        poll = self._knob("limit_poll_seconds", 1.0)
        while self._active is active:
            await asyncio.sleep(poll)
            if self._active is not active or active.pause is not None:
                continue
            now = time.monotonic()
            if active.armed_at is not None:
                limit = self._knob("counselling_limit_seconds", 0.0)
                if limit > 0 and now - active.armed_at >= limit:
                    await self._end_at_a_pause(active, "counselling_limit")
                    return
            else:
                limit = self._knob("no_activity_limit_seconds", 0.0)
                if limit > 0 and now - active.last_cmed_at >= limit:
                    await self._close_by_limit(active, "no_activity_limit")
                    return

    async def _end_at_a_pause(self, active: ActiveSession, reason: str) -> None:
        """
        The limit is reached: close at the first quiet moment within the grace,
        so the recording does not end in the middle of the doctor's sentence.
        """
        grace = self._knob("end_grace_seconds", 10.0)
        quiet = float(getattr(getattr(self.cfg, "segment", None), "silence_rms", 320))
        deadline = time.monotonic() + max(0.0, grace)
        step = min(0.2, max(0.01, grace / 50)) if grace > 0 else 0.0
        while time.monotonic() < deadline and self._active is active:
            if getattr(active.segmenter, "live_level", 0.0) < quiet:
                break
            await asyncio.sleep(step)
        await self._close_by_limit(active, reason)

    async def _close_by_limit(self, active: ActiveSession, reason: str) -> None:
        async with self._lock:
            if self._active is not active or active.pause is not None:
                return
            logger.info("Session %s ended by itself: %s", active.session_id, reason)
            self._forget()
            await self._close_active(reason=reason)

    def _background_task(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._on_background_done)
        return task

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
                    internal and reason in _INTERNAL_PAUSES):
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
            loop = asyncio.get_running_loop()
            stats = await loop.run_in_executor(None, active.recorder.stop)
            active.audio_seconds += stats.bytes_captured / max(1, active.recorder.bytes_per_second)
            await loop.run_in_executor(None, lambda: active.segmenter.flush(is_final=False))

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

    async def hold_for_shutdown(self) -> Dict[str, Any]:
        """
        Windows asked whether it may shut down. Stop the microphone and write the
        clip so far, but do not end the consultation: the shutdown may still be
        cancelled, and then it carries on.
        """
        active = self._active
        if active is None or self.state != RECORDING:
            return {"status": "unchanged", "session_id": active.session_id if active else None}
        result = await self.pause_session(
            reason=SHUTDOWN_HOLD_REASON, reason_detail="Windows asked to shut down",
            internal=True)
        active.shutdown_hold = True
        return result

    async def release_shutdown_hold(self) -> Dict[str, Any]:
        """The shutdown was cancelled: recording carries on."""
        active = self._active
        if active is None or not active.shutdown_hold:
            return {"status": "unchanged",
                    "session_id": active.session_id if active else None}
        active.shutdown_hold = False
        return await self.resume_session()

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

            # The microphone first. If it cannot open, nothing has changed: the
            # recording is still paused, the chain has no resume for audio that
            # never existed, and Resume can simply be pressed again.
            now = datetime.now(timezone.utc)
            active.segmenter.set_segment_start(now)
            try:
                await asyncio.get_running_loop().run_in_executor(None, active.recorder.start)
            except AudioCaptureError as exc:
                raise SessionError(
                    "The microphone could not be reopened. Check the device and try again."
                ) from exc

            paused_for = time.monotonic() - active.pause.started_monotonic
            active.paused_seconds += paused_for
            # A pause counts towards neither limit.
            active.last_cmed_at += paused_for
            if active.armed_at is not None:
                active.armed_at += paused_for

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
        # A deliberate stop ends the consultation: nothing to retry or resume.
        # Shutting the recorder down is not a stop - the consultation may well
        # carry on, and the restart picks it up.
        # Nor is a software update: the updated agent resumes the consultation.
        if reason not in ("agent_shutdown", "pc_shutdown", "software_update"):
            self._forget()
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

        # A held recording's loop sleeps for minutes between questions. Left
        # running past the close it outlived the consultation it was asking
        # about; the upload loop carries the asking on from here.
        if active.auth_task is not None and not active.auth_task.done():
            active.auth_task.cancel()
        if (active.limit_task is not None and not active.limit_task.done()
                and active.limit_task is not asyncio.current_task()):
            active.limit_task.cancel()

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

        # Off the event loop: joining the capture threads and writing the last
        # clip can take seconds (a microphone just pulled, a slow disk), and
        # meanwhile CMED's next message and the overlay must still be answered.
        loop = asyncio.get_running_loop()
        if active.recorder.is_running:
            stats = await loop.run_in_executor(None, active.recorder.stop)
            active.audio_seconds += stats.bytes_captured / max(1, active.recorder.bytes_per_second)
            self._check_capture_health(active, stats)

        # Seals the tail as the final segment.
        await loop.run_in_executor(None, lambda: active.segmenter.stop(seal_remaining=True))

        close_args = dict(duration_seconds=active.audio_seconds,
                          paused_seconds=active.paused_seconds,
                          reason=reason, detail=detail, at=at)
        if active.segmenter.pending_seconds > 0:
            # The disk is full: clips are still in memory, waiting. The close
            # entry must follow them in the chain, so it is written once they
            # are - in the background, so Stop answers now and the next patient
            # is not held up by this one.
            self._spawn(self._finish_close_later(active, close_args))
            return self._closed(active, reason, chain_ok=True, deferred=True)
        try:
            close_entry = active.spool.close(**close_args)
        except OSError as exc:
            self._report_disk_full(active.session_id, f"the close could not be written ({exc})")
            self._spawn(self._finish_close_later(active, close_args))
            return self._closed(active, reason, chain_ok=True, deferred=True)
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
        active.spool.live = False
        return self._closed(active, reason, chain_ok=verdict.ok)

    def _closed(self, active: ActiveSession, reason: str, *, chain_ok: bool,
                deferred: bool = False) -> Dict[str, Any]:
        result = {
            "status": "stopped",
            "session_id": active.session_id,
            "duration_seconds": round(active.audio_seconds, 1),
            "paused_seconds": round(active.paused_seconds, 1),
            "segment_count": len(active.spool.segments),
            "pauses": active.pauses,
            "reason": reason,
            "chain_ok": chain_ok,
        }
        if deferred:
            result["waiting_for_disk"] = True

        logger.info("Session %s stopped: %.1f s audio, %s segments, %s pause(s), reason=%s%s",
                    active.session_id, active.audio_seconds,
                    len(active.spool.segments), len(active.pauses), reason,
                    " (closing once the disk has room)" if deferred else "")

        self._active = None
        self.state = IDLE
        self._emit("recording_stopped", result)
        return result

    async def _finish_close_later(self, active: ActiveSession, close_args: Dict[str, Any]) -> None:
        """
        Close a recording whose last clips were waiting for disk space: once they
        are written, write the close, then report it. Tried every few seconds
        for as long as the agent runs; on a restart the interrupted-session
        recovery closes it instead, with nothing lost that reached the disk.
        """
        loop = asyncio.get_running_loop()
        while not await loop.run_in_executor(None, active.segmenter.wait_written, 5.0):
            pass
        active.segmenter.stop(seal_remaining=False)
        while True:
            try:
                active.spool.close(**close_args)
                break
            except OSError:
                await asyncio.sleep(5.0)
        verdict = active.spool.verify_chain()
        if not verdict.ok:
            self._emit("integrity_alert", {"session_id": active.session_id,
                                           "alert_type": "local_chain_invalid",
                                           "detail": verdict.reason})
        if active.segmenter.lost_seconds:
            self._emit("integrity_alert", {
                "session_id": active.session_id, "alert_type": "disk_full",
                "detail": f"{active.segmenter.lost_seconds:.0f} s of audio could not be kept: "
                          f"the disk stayed full past the memory limit"})
        active.spool.live = False
        await self._uploader.close_remote(active.spool, duration_seconds=active.audio_seconds,
                                          paused_seconds=active.paused_seconds)
        self._uploader.nudge()
        logger.info("Session %s closed after waiting for disk space", active.session_id)

    # ---- the disk ----

    def _report_disk_full(self, session_id: Optional[str], detail: str) -> None:
        """disk_full, once until the disk has room again. Any thread."""
        if self._disk_full_reported:
            return
        self._disk_full_reported = True
        logger.critical("DISK FULL: %s", detail)
        self._emit("integrity_alert", {"session_id": session_id, "alert_type": "disk_full",
                                       "detail": detail})

    def _disk_recovered(self, waited: float) -> None:
        self._disk_full_reported = False
        logger.warning("Disk writable again after %.0f s", waited)

    def _check_disk(self) -> Optional[int]:
        free = self._drive.free_bytes()
        if free is None:
            return None
        if free < MIN_FREE_BYTES:
            self._report_disk_full(self._active.session_id if self._active else None,
                                   f"only {free / 2**20:.0f} MB free on the PC's disk")
        elif self._disk_full_reported and free > 2 * MIN_FREE_BYTES:
            self._disk_full_reported = False
        return free

    async def refuse_session(self) -> Dict[str, Any]:
        """
        The patient did not consent (SRS 3.2 §7.8a).

        The microphone stops at once, every piece of this consultation is
        deleted from the PC, and the upload loop tells the server - which erases
        what it holds - until the server acknowledges (SRS-CNS-03..05).
        """
        async with self._lock:
            active = self._require_active()
            loop = asyncio.get_running_loop()
            if active.recorder.is_running:
                await loop.run_in_executor(None, active.recorder.stop)
            # Stop the cutter before deleting, so no piece lands afterwards.
            await loop.run_in_executor(None, lambda: active.segmenter.stop(seal_remaining=False))
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

    # ---- starts that failed, and restarts (nothing asked of the doctor) ----

    def _want(self, fields: Dict[str, str], *, session_id: Optional[str] = None) -> None:
        self._wanted = dict(fields)
        self._keeper.want(fields, session_id=session_id)

    def _forget(self) -> None:
        self._wanted = None
        self._keeper.forget()
        if self._start_retry is not None and not self._start_retry.done():
            self._start_retry.cancel()

    def _retry_start_later(self, trigger) -> None:
        if self._start_retry is not None and not self._start_retry.done():
            self._start_retry.cancel()
        task = asyncio.create_task(self._retry_start(trigger), name="StartRetry")
        self._start_retry = task
        self._background.add(task)
        task.add_done_callback(self._on_background_done)

    async def _retry_start(self, trigger) -> None:
        """
        Try the consultation CMED asked for again until it records, while it is
        still the one wanted: a stop, a refusal or another patient ends this.
        """
        began = time.monotonic()
        every = self._knob("start_retry_seconds", 5.0)
        limit = self._knob("start_retry_limit_seconds", 900.0)
        while time.monotonic() - began < limit:
            await asyncio.sleep(every)
            if self._active is not None or self._wanted != trigger.fields():
                return
            try:
                await self.open_session(trigger, retrying=True)
            except SessionError as exc:
                if exc.code == "AGENT_NOT_READY":
                    continue
                logger.warning("Retried start for %s refused: %s",
                               self._pseudonym(trigger.patient_id), exc.code)
                return
            late = time.monotonic() - began
            logger.warning("Consultation for %s started by itself %.0f s late",
                           self._pseudonym(trigger.patient_id), late)
            self._emit("integrity_alert", {
                "session_id": self._active.session_id if self._active else None,
                "alert_type": "start_recovered",
                "detail": f"the recording started by itself {late:.0f} s after CMED asked, "
                          f"once the microphone could be opened",
            })
            return
        self._forget()
        self._emit("integrity_alert", {
            "session_id": None,
            "alert_type": "start_abandoned",
            "detail": f"the microphone could not be opened for {limit / 60:.0f} minutes; "
                      f"this consultation was not recorded",
        })

    async def _resume_after_restart(self) -> None:
        """
        The recorder died mid-consultation and has been restarted. If that
        consultation was live moments ago, record it again under the same five
        fields - the doctor is almost certainly still with the patient.
        """
        wanted = self._keeper.recent(self._knob("resume_within_seconds", 600.0))
        if not wanted or self._active is not None:
            return
        from api.protocol import Trigger
        try:
            trigger = Trigger(**wanted["trigger"])
        except TypeError:
            self._forget()
            return
        self._wanted = trigger.fields()
        self._emit("integrity_alert", {
            "session_id": wanted.get("session_id"),
            "alert_type": "resumed_after_restart",
            "detail": f"the recorder restarted {wanted.get('age_seconds', 0):.0f} s after this "
                      f"consultation was last seen live; recording it again by itself",
        })
        try:
            await self.open_session(trigger, continues_session_id=wanted.get("session_id"))
        except SessionError as exc:
            if exc.code != "AGENT_NOT_READY":           # the microphone retries itself
                logger.warning("Could not resume after restart: %s", exc.code)
                self._forget()
            return
        if wanted.get("armed") and self._active is not None:
            self._active.armed = True
            # The prescription came before the restart; the counselling limit
            # counts again from here rather than closing at once.
            self._active.armed_at = time.monotonic()
            self._keeper.armed()
        logger.warning("Resumed consultation for %s after a restart",
                       self._pseudonym(trigger.patient_id))

    async def force_reset(self, *, actor: str = "unknown", reason: str = "") -> Dict[str, Any]:
        """
        Clear a stuck state without discarding audio.

        v1's force reset dropped the session and its recording. Here everything
        already sealed stays in the spool and continues uploading; only the live
        state machine is reset, and the event is recorded.
        """
        self._forget()
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

    def _seal_into(self, sink: SegmentSink, sealed: SealedSegment) -> None:
        """
        Runs on the segmenter thread. Writes one clip into the recording it was
        captured for, then wakes the uploader.

        Disk I/O here is deliberate: this thread exists so the capture thread does
        not have to do it. Any failure raises, so the segmenter keeps the clip and
        tries again: returning would tell it the clip was written.
        """
        spool = sink.spool
        if getattr(spool, "refused", False):
            # The patient refused: nothing of this consultation may be kept.
            # The segmenter was stopped before the refusal; this is a clip that
            # was already in flight, and it is dropped on purpose.
            logger.warning("Dropping a clip of refused session %s", spool.session_id)
            return
        segment = spool.seal_segment(
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
            sink.consecutive_silent += 1
            if sink.consecutive_silent >= 2 and not sink.silence_alerted:
                sink.silence_alerted = True
                self._emit("integrity_alert", {
                    "session_id": spool.session_id,
                    "seq_no": segment.seq_no,
                    "alert_type": "silent_session",
                    "detail": (f"{sink.consecutive_silent} consecutive segments at the "
                               f"noise floor (mean RMS {sealed.rms_mean:.1f}) - check that "
                               f"the microphone is connected and not muted"),
                })
        else:
            sink.consecutive_silent = 0

        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._uploader.nudge)
            except RuntimeError:                  # the loop has closed (shutdown)
                pass

        self._emit("segment_sealed", {
            "session_id": spool.session_id,
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

    def _on_microphone_switched(self, device: str, seconds_lost: float) -> None:
        """Called from the audio watchdog: the recording moved to another microphone."""
        active = self._active
        if active is not None:
            # The gap is real time with no audio. Without this every later clip's
            # timestamps would be early by the length of the gap.
            active.segmenter.set_segment_start(datetime.now(timezone.utc))
        self._emit("integrity_alert", {
            "session_id": active.session_id if active else None,
            "alert_type": "microphone_switched",
            "detail": (f"the microphone stopped; recording continued on {device} "
                       f"after about {seconds_lost:.0f}s"),
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

    @property
    def state(self) -> str:
        return self._state

    @state.setter
    def state(self, value: str) -> None:
        # Windows must not sleep while a consultation is open, however long the
        # doctor goes without touching the keyboard.
        self._state = value
        keep_awake(value != IDLE)

    async def _ready_microphone(self) -> None:
        """
        The clinic microphone at 100% and unmuted, just before a recording
        starts - never at sign-in or while idle, when changing a device's level
        is no business of a recorder that is not recording. Off the event loop,
        and never allowed to delay or stop the start.
        """
        name = getattr(self.cfg.audio, "preferred_microphone", "")
        if not name:
            return
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, make_ready, name), 1.0)
        except Exception as exc:
            logger.info("Could not set the microphone level before recording: %s", exc)

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
                if self._active is not None:
                    self._keeper.touch()           # still live, for a restart to see
                self._check_disk()
                payload = self.heartbeat_payload()
                health = getattr(self._uploader, "health", None)
                if await self._uploader.heartbeat(payload):
                    if health is not None:
                        await self._uploader.deliver_health()
                elif health is not None:
                    health.offline_heartbeat(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Heartbeat failed: %s", exc)

    def heartbeat_payload(self) -> Dict[str, Any]:
        upload = self._uploader.status()
        active = self._active
        held_of = getattr(self._uploader, "held_sessions", None)
        held = list(held_of()) if callable(held_of) else []
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
            # Whether the room was recording well, for the offline log: a
            # flat meter and two silent segments mean a muted microphone.
            "mic_level": self._live_level(active),
            "silent": bool(active is not None and active.consecutive_silent >= 2),
            "authorisation": active.authorisation if active is not None else None,
            # Server time minus this PC's time, as measured from the server's
            # replies; the server raises clock_skew when it is large.
            "clock_offset_seconds": (round(offset, 1) if (offset := getattr(
                self._uploader, "clock_offset", None)) is not None else None),
            # Recordings kept on this PC because the server said no, and why.
            "held": held[:20],
            "held_count": len(held),
            # BitLocker on the drive holding the audio: true, false, or unknown.
            "disk_encrypted": self._drive.encrypted,
            "disk_free_bytes": self._drive.free_bytes(),
            "sent_at": crypto.iso_utc(datetime.now(timezone.utc)),
        }

    # ---- status ----

    @staticmethod
    def _live_level(active) -> float:
        """What the microphone is hearing, scaled for a meter."""
        segmenter = getattr(active, "segmenter", None) if active else None
        if segmenter is None:
            return 0.0
        try:
            raw = float(segmenter.live_level)
        except Exception:
            return 0.0
        # 16-bit RMS runs to 32767, but a consulting room never approaches it:
        # quiet speech is around 300 and a raised voice around 4000, so the
        # meter is scaled to the room rather than to the format.
        return round(max(0.0, min(1.0, raw / 4000.0)), 3)

    def status(self) -> Dict[str, Any]:
        active = self._active
        upload = self._uploader.status()

        payload: Dict[str, Any] = {
            "state": self.state,
            "is_recording": self.state == RECORDING,
            "is_paused": self.state == PAUSED,
            "session_id": active.session_id if active else None,
            "patient_ref": active.patient_ref if active else None,
            # The patient CMED has open now; differs from patient_ref after a
            # click during an unarmed recording.
            "focus_patient": active.focus.get("patient_id") if active else None,
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
            # 0.0 to 1.0, for the meter on the on-screen control. Speech
            # sits around 0.3 to 0.7; a dead microphone stays at 0.0,
            # which is the thing a doctor most needs to be able to see.
            "level": self._live_level(active),
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
           "REFUSAL_REASON", "HOLD_MESSAGES", "STOP_HOLD_REASON"]
