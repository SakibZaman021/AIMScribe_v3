"""
A power cut costs seconds, not the minute of audio held in memory.

The clip being assembled is written, encrypted, to the session folder every few
seconds. After a power cut - including one that tears the last write in half -
the next start seals what was saved as the recording's final clip, in the chain.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

from core.simple_splitter import Segmenter
from core.spool import PARTIAL_NAME
from tests.conftest import pcm
from tests.fakes import ScriptedUploader, make_controller

START = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)
BPS = 44100 * 2


def open_spool_session(spool, device_key, audio_params):
    return spool.open_session(
        device_key=device_key, device_id="dev-1", doctor_id="DR001",
        hospital_id="HOSP001", patient_ref="P12345", consent_method="reception",
        audio=audio_params)


def tone(seconds, value):
    return pcm(seconds, value=value)


# ---------------------------------------------------------------- segmenter

def test_the_clip_in_progress_is_handed_out_every_few_seconds_and_reset_on_seal():
    calls, sealed = [], []
    seg = Segmenter(sample_rate=44100, channels=1, sample_width=2, min_seconds=30,
                    max_seconds=60, silence_rms=320, silence_hold_seconds=1.0,
                    on_segment=sealed.append, partial_every_seconds=3.0,
                    on_partial=lambda kind, data, at: calls.append((kind, len(data))))
    seg.start(START)
    for _ in range(10):                         # 10 s in one-second chunks
        seg.submit(tone(1.0, 2000))
    seg.flush(is_final=False)
    seg.stop(seal_remaining=False)
    appends = [n for kind, n in calls if kind == "append"]
    assert appends == [3 * BPS, 3 * BPS, 3 * BPS]          # every 3 s, nothing twice
    assert calls[-1] == ("reset", 0) and len(sealed) == 1  # sealed: file emptied


def test_without_a_callback_nothing_changes():
    seg = Segmenter(sample_rate=44100, channels=1, sample_width=2, min_seconds=30,
                    max_seconds=60, silence_rms=320, silence_hold_seconds=1.0,
                    on_segment=lambda s: None)
    assert seg._partial_bytes == 0


# ---------------------------------------------------------------- spool

def test_a_power_cut_mid_write_keeps_every_whole_record(spool, device_key, audio_params):
    session = open_spool_session(spool, device_key, audio_params)
    for value in (100, 200, 300):
        session.write_partial("append", tone(3.0, value), START)
    path = session.directory / PARTIAL_NAME
    raw = path.read_bytes()
    assert tone(3.0, 100)[:64] not in raw                      # encrypted at rest
    path.write_bytes(raw[:-1000])                              # the last write torn

    started, audio = session.read_partial()
    assert started == START
    assert audio == tone(3.0, 100) + tone(3.0, 200)            # third lost, rest kept


def test_a_reset_starts_the_file_again_and_an_empty_one_removes_it(spool, device_key,
                                                                   audio_params):
    session = open_spool_session(spool, device_key, audio_params)
    session.write_partial("append", tone(3.0, 100), START)
    later = START + timedelta(seconds=40)
    session.write_partial("reset", tone(0.5, 7), later)
    session.write_partial("append", tone(3.0, 8), later)
    assert session.read_partial() == (later, tone(0.5, 7) + tone(3.0, 8))
    session.write_partial("reset", b"", later)
    assert not (session.directory / PARTIAL_NAME).exists()
    assert session.read_partial() is None


def test_a_clip_sealed_just_before_the_cut_is_not_saved_twice(spool, device_key,
                                                              audio_params):
    """Sealed, then the power went before the file was started again."""
    session = open_spool_session(spool, device_key, audio_params)
    session.write_partial("append", tone(3.0, 100) + tone(1.0, 200), START)
    session.seal_segment(tone(3.0, 100), captured_start_at=START,
                         captured_end_at=START + timedelta(seconds=3), rms_mean=100,
                         is_final=False)
    started, audio = session.read_partial()
    assert audio == tone(1.0, 200) and started == START + timedelta(seconds=3)


# ---------------------------------------------------------------- recovery

async def test_the_next_start_seals_what_was_saved_into_the_chain(
        monkeypatch, spool, device_key, audio_params):
    session = open_spool_session(spool, device_key, audio_params)
    session.seal_segment(tone(30.0, 500), captured_start_at=START,
                         captured_end_at=START + timedelta(seconds=30), rms_mean=500,
                         is_final=False)
    after = START + timedelta(seconds=30)
    session.write_partial("reset", tone(2.0, 900), after)
    session.write_partial("append", tone(3.0, 900), after)
    raw = (session.directory / PARTIAL_NAME).read_bytes()
    (session.directory / PARTIAL_NAME).write_bytes(raw[:-10])     # torn last write

    ctl = make_controller(monkeypatch, spool, device_key, ScriptedUploader())
    await ctl._recover_previous_sessions()

    [recovered] = [s for s in spool.recover(device_key) if s.session_id == session.session_id]
    final = recovered.segments[max(recovered.segments)]
    assert final.is_final and abs(final.duration_seconds - 2.0) < 1e-6
    assert recovered.read_segment(final.seq_no)[44:] == tone(2.0, 900)
    assert final.captured_start_at == after
    assert recovered.verify_chain().ok
    assert recovered.closed_at is not None
    assert not (recovered.directory / PARTIAL_NAME).exists()
    alert = [d for e, d in ctl.events if e == "integrity_alert"][-1]
    assert "2.0s of the clip in progress were saved" in alert["detail"]
    await ctl.close()


def test_flushing_costs_almost_nothing(spool, device_key, audio_params):
    """Three seconds of audio, encrypted and fsync'd, once every three seconds."""
    session = open_spool_session(spool, device_key, audio_params)
    chunk = tone(3.0, 1234)
    began = time.perf_counter()
    for _ in range(20):
        session.write_partial("append", chunk, START)
    per_flush = (time.perf_counter() - began) / 20
    assert per_flush < 0.1                    # well under 3% of a 3 s interval
    print(f"flush: {per_flush * 1000:.1f} ms per 3 s of audio")
