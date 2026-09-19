"""Stand-ins shared by the Channel A and authorisation tests."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from api.protocol import Trigger


def trigger(patient="P1", start="2026-09-13T10:00:00+06:00", doctor="DR0042") -> Trigger:
    return Trigger(patient_id=patient, doctor_id=doctor, hospital_id="CMED-DHK-BANANI-01",
                   start_time=start, date="2026-09-13")


class FakeRecorder:
    def __init__(self, **kwargs):
        self.is_running = False
        self.bytes_per_second = 88200
        self.duration_seconds = 0.0

    def start(self):
        self.is_running = True

    def stop(self):
        self.is_running = False
        return SimpleNamespace(bytes_captured=0, overruns=0, read_errors=0)


class FakeSegmenter:
    def __init__(self, **kwargs):
        self.stopped_with = None

    def start(self, at):
        pass

    def stop(self, seal_remaining=True):
        self.stopped_with = seal_remaining

    def flush(self, is_final=False):
        pass

    def set_segment_start(self, at):
        pass

    def submit(self, chunk):
        pass


class ScriptedUploader:
    """Answers grant requests from a script, the way the real uploader records them."""

    def __init__(self, outcomes=(), *, delay=0.0):
        self.outcomes = list(outcomes)
        self.delay = delay
        self.calls = 0
        self.confirmations = []
        self.closed = []
        self.forgotten = []

    async def authorise(self, session):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if outcome.status == "granted":
            session.record_authorised("jti-test", outcome.confirmation)
        elif outcome.status == "refused":
            session.record_authorisation_refused(outcome.code)
        return outcome

    async def notify_pause(self, session, entry):
        return True

    async def notify_resume(self, session, entry):
        return True

    async def check_confirmation(self, session):
        return self.confirmations.pop(0) if self.confirmations else None

    async def track(self, session):
        pass

    def nudge(self):
        pass

    async def close_remote(self, session, **kwargs):
        self.closed.append(session.session_id)

    async def forget(self, session):
        self.forgotten.append(session.session_id)

    def status(self):
        return {"spool_bytes": 0, "spool_pressure": "ok", "pending_segments": 0,
                "oldest_pending_seconds": 0}


def controller_config(**security):
    knobs = dict(authorise_wait_seconds=0.3, authorise_retry_seconds=0.01,
                 confirm_poll_seconds=0.01, confirm_deadline_seconds=5.0)
    knobs.update(security)
    return SimpleNamespace(
        audio=SimpleNamespace(sample_rate=44100, channels=1, sample_width=2,
                              bytes_per_second=88200, frames_per_buffer=4096,
                              input_device_index=None),
        segment=SimpleNamespace(min_seconds=30, max_seconds=60, grace_seconds=15,
                                silence_rms=320, silence_hold_seconds=3.0),
        ops=SimpleNamespace(redact_logs=True, heartbeat_seconds=30),
        pause=SimpleNamespace(reasons=("other",), self_authorise_seconds=300),
        security=SimpleNamespace(**knobs),
        spool_seconds=lambda: 4 * 1024 ** 3 / 88200,
    )


def make_controller(monkeypatch, spool, device_key, uploader, **security):
    import core.session_controller as sc
    monkeypatch.setattr(sc, "AudioRecorder", FakeRecorder)
    monkeypatch.setattr(sc, "Segmenter", FakeSegmenter)
    events = []
    ctl = sc.SessionController(controller_config(**security), device_key=device_key,
                               spool=spool, uploader=uploader,
                               on_event=lambda name, data: events.append((name, data)))
    ctl.device_id = "DEV1"
    ctl.hospital_id = "HOSP003"
    ctl.events = events
    return ctl
