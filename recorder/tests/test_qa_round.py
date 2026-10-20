"""
Regression tests for the QA review of the recorder (round 1): one per finding.

  1  clips waiting for disk space are written into their own recording
  2  a Resume that cannot open the microphone leaves the pause intact
  3  a cancelled Windows shutdown resumes the recording
  4  one failing upload no longer stalls every session; jitter; Retry-After
  5  Stop does not freeze the event loop while the microphone is released
  6  an interrupted recording that cannot be closed at startup does not stop
     the agent starting
  7  the upload loop reads a snapshot of the spool, not the live dict
  8  a page in another Windows user's session is refused
  9  abandoned streams are reaped; the used-grants file is written durably
 11  the refusal is signed with the PC's key
 12  every request to our server is signed
 13  TLS interception is detected, and refused when the server is pinned
     the installer's stop event closes the recording and quits the agent
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import core.session_controller as sc
import core.simple_splitter as ss
from core import crypto, peer_session, stop_event, tls_check
from core.recorder import AudioCaptureError
from core.session_controller import SessionError
from core.spool import SessionSpool
from core.start_keeper import StartKeeper
from core.uploader import (AuthOutcome, UploadManager, jittered, retry_after_seconds,
                           sign_request, signing_string)
from tests.fakes import FakeRecorder, ScriptedUploader, make_controller, trigger

GRANTED = AuthOutcome("granted", "GRANTED", "", None, "confirmed")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 1

def test_clips_waiting_for_disk_space_stay_in_their_own_recording(monkeypatch, tmp_path,
                                                                    spool, device_key):
    async def body():
        ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED]))
        ctl._keeper = StartKeeper(tmp_path / "state")

        class Seg(ss.Segmenter):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.retry_seconds = 0.2
        monkeypatch.setattr(sc, "Segmenter", Seg)
        ctl.cfg.segment.partial_flush_seconds = 0
        disk = {"full": True}
        original = SessionSpool.seal_segment

        def seal(self, *a, **k):
            if disk["full"]:
                raise OSError(28, "No space left on device")
            return original(self, *a, **k)
        monkeypatch.setattr(SessionSpool, "seal_segment", seal)

        a = await ctl.open_session(trigger("P1"))
        ctl._active.segmenter.submit(b"\x10\x20" * 88200)        # P1's audio
        stopped = await ctl.stop_session()
        assert stopped.get("waiting_for_disk") is True
        b = await ctl.open_session(trigger("P2", start="2026-09-13T10:05:00+06:00"))
        disk["full"] = False
        for _ in range(100):
            await asyncio.sleep(0.05)
            if next(s for s in spool.recover(device_key)
                    if s.session_id == a["session_id"]).segments:
                break
        found = {s.session_id: s for s in spool.recover(device_key)}
        assert len(found[a["session_id"]].segments) >= 1, "P1's audio was lost"
        assert len(found[b["session_id"]].segments) == 0, "P1's audio went to P2"
        await ctl.stop_session()
        await ctl.close()
    run(body())


# ---------------------------------------------------------------- 2

def test_a_failed_resume_can_be_tried_again(monkeypatch, tmp_path, spool, device_key):
    async def body():
        ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED]))
        ctl._keeper = StartKeeper(tmp_path / "state")
        await ctl.open_session(trigger("P1"))
        await ctl.pause_session(reason="other", reason_detail="x")
        active = ctl._active
        entries = len(active.spool.chain)
        recorder = active.recorder

        def unplugged():
            raise AudioCaptureError("gone")
        recorder.start = unplugged
        with pytest.raises(SessionError):
            await ctl.resume_session()
        assert ctl.state == sc.PAUSED and active.pause is not None
        assert len(active.spool.chain) == entries            # no resume for nothing
        recorder.start = lambda: setattr(recorder, "is_running", True)
        assert (await ctl.resume_session())["status"] == "recording"
        assert active.spool.chain[-1].entry_type == "resume"
        await ctl.stop_session()
    run(body())


# ---------------------------------------------------------------- 3

def test_a_cancelled_shutdown_carries_the_recording_on(monkeypatch, tmp_path, spool,
                                                       device_key):
    async def body():
        ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED]))
        ctl._keeper = StartKeeper(tmp_path / "state")
        opened = await ctl.open_session(trigger("P1"))
        await ctl.hold_for_shutdown()                        # Windows asks
        assert ctl.state == sc.PAUSED
        await ctl.release_shutdown_hold()                    # ... then cancels
        assert ctl.state == sc.RECORDING
        assert ctl._active.session_id == opened["session_id"]
        kinds = [e.entry_type for e in ctl._active.spool.chain]
        assert kinds[-2:] == ["pause", "resume"]
        await ctl.stop_session()
    run(body())


# ---------------------------------------------------------------- 4

def bare_uploader(**backend):
    cfg = SimpleNamespace(
        backend=SimpleNamespace(url=lambda e: "https://aims.example/api/v2" + e,
                                base_url="https://aims.example", request_timeout=5,
                                upload_timeout=5, retry_backoff=(0.0,), **backend),
        spool=SimpleNamespace(purge_grace_hours=0), app_version="3.5.0", protocol_version=2)
    return UploadManager(cfg, device_key=None, spool=None, receipt_public_key=None)


class _Response:
    def __init__(self, status, headers=None):
        self.status, self.headers = status, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return ""


def test_an_object_upload_is_tried_once_per_pass():
    up = bare_uploader()
    calls = []

    class Http:
        def put(self, url, **kw):
            calls.append(url)
            return _Response(503)
    up._http = Http()
    assert run(up._put_object("https://r2.example/x", b"audio")) is False
    assert calls == ["https://r2.example/x"]
    assert up.consecutive_failures == 1


def test_one_sessions_failure_does_not_stop_the_others():
    up = bare_uploader()
    up._running = True
    seen = []

    bad, good = SimpleNamespace(session_id="bad"), SimpleNamespace(session_id="good")

    async def drain(session):
        seen.append(session.session_id)
        if session is bad:
            raise RuntimeError("boom")
    up._drain_session = drain
    up._sessions = [bad, good]
    up._check_stall = lambda: None
    run(up._drain_once())
    assert seen == ["bad", "good"]


def test_backoff_is_jittered_and_retry_after_is_honoured_and_capped():
    assert all(0.0 <= jittered(30.0) <= 30.0 for _ in range(200))
    assert len({round(jittered(30.0), 3) for _ in range(50)}) > 10
    assert retry_after_seconds("12") == 12.0
    assert retry_after_seconds("99999") == 300.0
    assert retry_after_seconds("nonsense") is None and retry_after_seconds(None) is None


def test_a_429_keeps_the_pc_quiet_for_the_time_asked():
    up = bare_uploader()
    up._note_retry_after(_Response(429, {"Retry-After": "60"}))
    assert up._quiet()
    assert run(up._request_raw("GET", "/x", None)) == (None, None)


# ---------------------------------------------------------------- 5

def test_stop_does_not_freeze_the_event_loop(monkeypatch, tmp_path, spool, device_key):
    async def body():
        ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED]))
        ctl._keeper = StartKeeper(tmp_path / "state")
        await ctl.open_session(trigger("P1"))
        recorder = ctl._active.recorder
        real_stop = recorder.stop

        def slow_stop():
            time.sleep(0.6)                      # a microphone that was just pulled
            return real_stop()
        recorder.stop = slow_stop
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1
        task = asyncio.create_task(ticker())
        await ctl.stop_session()
        task.cancel()
        assert ticks >= 5, "the event loop was blocked during Stop"
    run(body())


# ---------------------------------------------------------------- 6

def test_a_full_disk_at_startup_does_not_stop_the_agent(monkeypatch, tmp_path, spool,
                                                        device_key):
    async def body():
        left = spool.open_session(device_key=device_key, device_id="D", doctor_id="DR",
                                  hospital_id="H", patient_ref="P",
                                  consent_method="reception",
                                  audio={"sample_rate": 4000, "channels": 1,
                                         "sample_width": 2})
        assert left.closed_at is None
        ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED]))
        ctl._keeper = StartKeeper(tmp_path / "state")
        monkeypatch.setattr(sc, "RECOVERY_RETRY_SECONDS", 0.05)
        disk = {"full": True}
        original = SessionSpool.append_chain_entry

        def append(self, *a, **k):
            if disk["full"]:
                raise OSError(28, "No space left on device")
            return original(self, *a, **k)
        monkeypatch.setattr(SessionSpool, "append_chain_entry", append)
        await ctl.start()                                    # must not raise
        assert any(d.get("alert_type") == "disk_full" for _, d in ctl.events)
        disk["full"] = False
        for _ in range(60):
            await asyncio.sleep(0.05)
            if next(s for s in spool.recover(device_key)
                    if s.session_id == left.session_id).closed_at is not None:
                break
        assert next(s for s in spool.recover(device_key)
                    if s.session_id == left.session_id).closed_at is not None
        await ctl.close()
    run(body())


# ---------------------------------------------------------------- 7

def test_the_upload_loop_reads_a_snapshot(spool, device_key):
    session = spool.open_session(device_key=device_key, device_id="D", doctor_id="DR",
                                 hospital_id="H", patient_ref="P", consent_method="reception",
                                 audio={"sample_rate": 4000, "channels": 1, "sample_width": 2})
    now = datetime.now(timezone.utc)
    session.seal_segment(b"\x00\x01" * 4000, captured_start_at=now,
                         captured_end_at=now + timedelta(seconds=1), rms_mean=100.0,
                         is_final=False)
    listed = session.segment_list()
    assert listed is not session.segments and len(listed) == 1
    session.seal_segment(b"\x00\x01" * 4000, captured_start_at=now,
                         captured_end_at=now + timedelta(seconds=1), rms_mean=100.0,
                         is_final=True)
    assert len(listed) == 1 and len(session.segment_list()) == 2


# ---------------------------------------------------------------- 8

def test_a_page_in_another_windows_session_is_refused(monkeypatch):
    from api.websocket_server import WebSocketManager
    closed = []

    class Socket:
        headers = {"origin": "https://cmed.example", "host": "127.0.0.1:5050"}
        client = SimpleNamespace(host="127.0.0.1", port=50001)
        scope = {"server": ("127.0.0.1", 5050)}

        async def close(self, code, reason):
            closed.append(reason)

    cfg = SimpleNamespace(security=SimpleNamespace(origin_allowed=lambda o: True,
                                                   host_allowed=lambda h: True))
    monkeypatch.setattr(peer_session, "same_session", lambda *a: False)
    assert run(WebSocketManager(cfg).connect(Socket())) is False
    assert closed == ["other windows session"]


def test_this_sessions_own_connection_is_recognised():
    import socket
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows only")
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    conn, addr = server.accept()
    try:
        assert peer_session.session_of_peer(addr[0], addr[1], port) == \
            peer_session.own_session()
        assert peer_session.same_session(addr[0], addr[1], port)
    finally:
        for s in (conn, client, server):
            s.close()


# ---------------------------------------------------------------- 9

def test_abandoned_streams_are_closed_once_their_reader_returns():
    from core.recorder import AudioRecorder
    rec = AudioRecorder.__new__(AudioRecorder)
    closed = []
    alive = threading.Event()
    stuck = threading.Thread(target=alive.wait)
    stuck.start()
    done = threading.Thread(target=lambda: None)
    done.start()
    done.join()
    stream = lambda name: SimpleNamespace(close=lambda: closed.append(name))  # noqa: E731
    rec._abandoned = [(stream("stuck"), None, stuck), (stream("returned"), None, done)]
    rec._reap_abandoned()
    assert closed == ["returned"] and len(rec._abandoned) == 1
    alive.set()
    stuck.join()
    rec._reap_abandoned()
    assert closed == ["returned", "stuck"] and rec._abandoned == []


def test_used_grants_are_written_durably(tmp_path, monkeypatch):
    import os
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real(fd))
    guard = crypto.GrantGuard(tmp_path / "used_grants.json")
    guard._seen_wall = {"jti-1": time.time()}
    guard._save()
    assert synced, "the file was not flushed to disk before the rename"
    assert json.loads((tmp_path / "used_grants.json").read_text())["used"]["jti-1"]


# ---------------------------------------------------------------- 11, 12

class _Key:
    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self.private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))

    def sign(self, message):
        return self.private.sign(message)

    def verify(self, signature, message):
        self.private.public_key().verify(signature, message)


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def test_the_refusal_is_signed_over_its_canonical_json():
    up = bare_uploader()
    up._device_key = _Key()
    up.device_id = "dev-1"
    at = datetime(2026, 9, 13, 4, 30, tzinfo=timezone.utc)
    body = up.refusal_body("S1", refused_at=at)
    assert body["refusal"] == {"session_id": "S1", "device_id": "dev-1",
                               "refused_at": "2026-09-13T04:30:00.000Z"}
    signed = crypto.canonical_json(body["refusal"])
    # The test vector the server checks against (backend integrity.canonical_json).
    assert signed == (b'{"device_id":"dev-1","refused_at":"2026-09-13T04:30:00.000Z",'
                      b'"session_id":"S1"}')
    up._device_key.verify(_unb64(body["signature"]), signed)


def test_the_request_signature_covers_method_path_time_nonce_and_body():
    key = _Key()
    body = b'{"seq_no":1}'
    headers = sign_request(key, "post", "https://aims.example/api/v2/segment/commit?x=1",
                           body, now=1790000000.7)
    assert headers["X-Device-Timestamp"] == "1790000000"
    assert len(headers["X-Device-Nonce"]) == 22
    expected = (b"AIMS-DEVICE-SIG-V1\nPOST\n/api/v2/segment/commit?x=1\n1790000000\n"
                + headers["X-Device-Nonce"].encode() + b"\n"
                + hashlib.sha256(body).hexdigest().encode())
    assert signing_string("POST", "/api/v2/segment/commit?x=1", "1790000000",
                          headers["X-Device-Nonce"], body) == expected
    key.verify(_unb64(headers["X-Device-Signature"]), expected)


def test_requests_carry_the_token_and_a_fresh_signature():
    up = bare_uploader()
    up._device_key = _Key()
    up._device_token = "tok"
    data, headers = up._prepare("POST", "https://aims.example/api/v2/heartbeat", {"a": 1})
    assert data == b'{"a":1}' and headers["X-Device-Token"] == "tok"
    assert headers["Content-Type"] == "application/json"
    again = up._prepare("POST", "https://aims.example/api/v2/heartbeat", {"a": 1})[1]
    assert again["X-Device-Nonce"] != headers["X-Device-Nonce"]


# ---------------------------------------------------------------- 13

def _certificate(issuer_org: str):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, issuer_org),
                      x509.NameAttribute(NameOID.COMMON_NAME, "aims.example")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(datetime(2026, 1, 1)).not_valid_after(datetime(2027, 1, 1))
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.DER)


def test_a_pinned_server_with_another_key_is_refused():
    real, other = _certificate("Let's Encrypt"), _certificate("Let's Encrypt")
    pin = tls_check.spki_pin(real)
    assert tls_check.judge(real, [pin]).ok
    verdict = tls_check.judge(other, [pin])
    assert not verdict.ok and "TLS interception detected" in verdict.problem


def test_https_inspection_is_reported_even_without_pins():
    verdict = tls_check.judge(_certificate("Kaspersky Lab ZAO"), [])
    assert verdict.ok and "TLS interception detected" in verdict.problem
    assert tls_check.judge(_certificate("Let's Encrypt"), []).problem == ""


def test_nothing_is_sent_when_the_pin_fails():
    up = bare_uploader()

    class Guard:
        problem = "TLS interception detected: x"

        async def allowed(self):
            return False
    up._tls = Guard()
    up._http = object()
    events = []
    up._on_event = lambda name, data: events.append(data)
    assert run(up._request("GET", "/x")) is None
    assert events[0]["alert_type"] == "tls_interception"
    assert up.tls_problem.startswith("TLS interception detected")


# ---------------------------------------------------------------- the installer's stop

def test_the_installers_stop_event_closes_and_quits():
    signalled = threading.Event()

    class Event:
        def wait(self, ms):
            return signalled.wait(ms / 1000)

        def close(self):
            pass
    fired = threading.Event()
    listener = stop_event.StopListener(fired.set, event=Event())
    assert listener.start()
    signalled.set()
    assert fired.wait(3) and listener.fired


def test_stopping_for_an_update_keeps_the_recording_and_quits():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    reasons, quits = [], []

    async def stop_session(reason):
        reasons.append(reason)
    runtime = SimpleNamespace(loop=loop, controller=SimpleNamespace(stop_session=stop_session))
    try:
        assert stop_event.stop_for_update(runtime, lambda: quits.append(1), timeout=3)
        assert reasons == ["software_update"] and quits == [1]
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()


def test_the_event_is_created_with_its_access_list(monkeypatch):
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows only")
    import ctypes
    name = "Global\\AIMScribeAgentStopTest-" + str(time.time_ns())
    event = stop_event.Win32Event(name=name)
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenEventW.restype = ctypes.c_void_p
        handle = kernel32.OpenEventW(0x0002, False, name)      # EVENT_MODIFY_STATE
        assert handle, "the owner could not open its own event"
        kernel32.SetEvent(ctypes.c_void_p(handle))
        assert event.wait(500) and not event.wait(50)          # auto-reset
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    finally:
        event.close()


def test_a_software_update_is_not_a_stop_that_forgets_the_consultation(monkeypatch, tmp_path,
                                                                        spool, device_key):
    async def body():
        ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader([GRANTED]))
        ctl._keeper = StartKeeper(tmp_path / "state")
        await ctl.open_session(trigger("P1"))
        forgotten = []
        monkeypatch.setattr(ctl, "_forget", lambda: forgotten.append(1))
        await ctl.stop_session(reason="software_update")
        assert forgotten == []
    run(body())
