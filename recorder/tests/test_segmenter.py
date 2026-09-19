"""
Segmenting behaviour - clips stream as they are produced (R3).

Also covers the structural fix that matters most for audio quality: the capture
thread only enqueues, and never blocks.
"""
from __future__ import annotations

import queue
from array import array
import threading
import time
from datetime import datetime, timezone

import pytest

from core.simple_splitter import SealedSegment, Segmenter
from tests.conftest import pcm

SAMPLE_RATE = 44100
BYTES_PER_SECOND = SAMPLE_RATE * 2


def _segmenter(collected, **overrides):
    settings = dict(
        sample_rate=SAMPLE_RATE, channels=1, sample_width=2,
        min_seconds=2.0, max_seconds=3.0,
        silence_rms=320, silence_hold_seconds=0.2,
        on_segment=collected.append,
    )
    settings.update(overrides)
    return Segmenter(**settings)


def _feed(segmenter: Segmenter, seconds: float, *, level: int, chunk: float = 0.1):
    """Push audio in realistic chunk sizes."""
    remaining = seconds
    while remaining > 1e-9:
        step = min(chunk, remaining)
        segmenter.submit(pcm(step, sample_rate=SAMPLE_RATE, value=level))
        remaining -= step


# ============================================================
# Boundaries
# ============================================================

def test_no_cut_before_the_minimum():
    collected = []
    segmenter = _segmenter(collected)
    segmenter.start(datetime.now(timezone.utc))
    try:
        # Silent, but under the minimum - must not cut.
        _feed(segmenter, 1.5, level=0)
        time.sleep(0.3)
        assert collected == []
    finally:
        segmenter.stop(seal_remaining=False)


def test_silence_cuts_inside_the_window():
    collected = []
    segmenter = _segmenter(collected)
    segmenter.start(datetime.now(timezone.utc))
    try:
        _feed(segmenter, 2.2, level=8000)   # loud, past the minimum
        _feed(segmenter, 0.4, level=0)      # sustained quiet -> cut
        deadline = time.time() + 3
        while not collected and time.time() < deadline:
            time.sleep(0.05)
        assert len(collected) == 1
        assert not collected[0].is_final
    finally:
        segmenter.stop(seal_remaining=False)


def test_continuous_speech_is_cut_at_the_maximum():
    """A doctor who never pauses must still produce regular clips."""
    collected = []
    segmenter = _segmenter(collected)
    segmenter.start(datetime.now(timezone.utc))
    try:
        _feed(segmenter, 3.2, level=12000)  # never quiet
        deadline = time.time() + 3
        while not collected and time.time() < deadline:
            time.sleep(0.05)
        assert len(collected) == 1
        duration = len(collected[0].pcm) / BYTES_PER_SECOND
        assert 2.9 <= duration <= 3.15
    finally:
        segmenter.stop(seal_remaining=False)


# ============================================================
# Flush ordering - pause boundaries must be exact
# ============================================================

def test_flush_seals_everything_queued_before_it():
    collected = []
    segmenter = _segmenter(collected)
    segmenter.start(datetime.now(timezone.utc))
    try:
        _feed(segmenter, 1.0, level=5000)
        assert segmenter.flush(is_final=False, timeout=5)
        assert len(collected) == 1
        assert pytest.approx(len(collected[0].pcm) / BYTES_PER_SECOND, abs=0.05) == 1.0
    finally:
        segmenter.stop(seal_remaining=False)


def test_flush_with_empty_buffer_emits_nothing():
    """Pausing immediately after a cut must not produce a zero-length segment."""
    collected = []
    segmenter = _segmenter(collected)
    segmenter.start(datetime.now(timezone.utc))
    try:
        assert segmenter.flush(is_final=False, timeout=5)
        assert collected == []
    finally:
        segmenter.stop(seal_remaining=False)


def test_stop_seals_the_tail_as_final():
    collected = []
    segmenter = _segmenter(collected)
    segmenter.start(datetime.now(timezone.utc))
    _feed(segmenter, 0.8, level=6000)
    segmenter.stop(seal_remaining=True, timeout=5)

    assert len(collected) == 1
    assert collected[0].is_final


def test_segment_start_moves_on_resume():
    """
    After a pause the next segment must be stamped from the resume time, not from
    where the previous one ended - otherwise the timestamps imply audio that was
    never recorded.
    """
    collected = []
    segmenter = _segmenter(collected)
    segmenter.start(datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc))
    try:
        _feed(segmenter, 0.5, level=6000)
        segmenter.flush(is_final=False, timeout=5)

        resumed = datetime(2026, 7, 26, 9, 5, tzinfo=timezone.utc)
        segmenter.set_segment_start(resumed)
        _feed(segmenter, 0.5, level=6000)
        segmenter.flush(is_final=False, timeout=5)

        assert len(collected) == 2
        assert collected[1].captured_start_at == resumed
        gap = collected[1].captured_start_at - collected[0].captured_end_at
        assert gap.total_seconds() > 250      # the pause is visible in the timestamps
    finally:
        segmenter.stop(seal_remaining=False)


# ============================================================
# The capture thread must never block
# ============================================================

def test_submit_raises_rather_than_blocking_when_full():
    """
    Blocking here would stall PortAudio and lose the same audio with worse timing,
    so a full queue must surface as an error the recorder can count.
    """
    segmenter = _segmenter([], queue_depth=2)
    # Deliberately not started: nothing drains the queue.
    segmenter.submit(b"\x00" * 10)
    segmenter.submit(b"\x00" * 10)

    with pytest.raises(queue.Full):
        segmenter.submit(b"\x00" * 10)


def test_submit_is_fast_enough_for_the_audio_thread():
    """One submit must cost far less than the 46 ms a buffer represents."""
    segmenter = _segmenter([], queue_depth=4096)
    payload = pcm(0.046, sample_rate=SAMPLE_RATE, value=1000)

    start = time.perf_counter()
    for _ in range(500):
        segmenter.submit(payload)
    per_call_ms = (time.perf_counter() - start) / 500 * 1000

    assert per_call_ms < 1.0


def test_rms_runs_off_the_capture_thread():
    """
    Loudness maths happens on the segmenter thread. Verified by observing that the
    callback runs on a different thread from the one that submitted.
    """
    seen = {}

    def record(segment: SealedSegment):
        seen["thread"] = threading.current_thread().name

    segmenter = _segmenter([], on_segment=record)
    segmenter.start(datetime.now(timezone.utc))
    submitting = threading.current_thread().name
    try:
        _feed(segmenter, 0.5, level=6000)
        segmenter.flush(is_final=False, timeout=5)
        assert seen.get("thread") == "Segmenter"
        assert seen["thread"] != submitting
    finally:
        segmenter.stop(seal_remaining=False)



# ============================================================
# Where the cut lands
#
# Clips are short now - 30 to 60 seconds - so the segmenter cuts three times as
# often as it used to, and every cut is a chance to slice a word in half. The
# server stitches the clips back together, so a badly placed cut costs nothing
# in audio; it costs a mangled word in the transcript, which is what the doctor
# reads.
# ============================================================

def _speech(seconds: float, *, level: int = 12000, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Alternating samples: loud, and busy with zero crossings, like a vowel."""
    frames = int(seconds * sample_rate)
    out = array("h", [level if n % 8 < 4 else -level for n in range(frames)])
    return out.tobytes()


def _fricative(seconds: float, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """
    Quiet but crossing zero on almost every sample - an s or sh sound.

    Loudness alone cannot tell this from a pause, which is how a cut ends up in
    the middle of "diabetes".
    """
    frames = int(seconds * sample_rate)
    out = array("h", [900 if n % 2 else -900 for n in range(frames)])
    return out.tobytes()


def _room_tone(seconds: float, *, level: int = 120, sample_rate: int = SAMPLE_RATE) -> bytes:
    """A real room is never digitally silent: fans, corridors, mains hum."""
    frames = int(seconds * sample_rate)
    out = array("h", [level if (n // 37) % 2 else -level for n in range(frames)])
    return out.tobytes()


def _wait_for(collected, count=1, timeout=4.0):
    deadline = time.time() + timeout
    while len(collected) < count and time.time() < deadline:
        time.sleep(0.05)
    return collected


def test_a_cut_lands_inside_the_pause_not_at_its_edges():
    """
    The clip should end part-way through the gap, so the words before it are
    complete and the next clip does not open on a word already in progress.
    """
    collected = []
    segmenter = _segmenter(collected, min_seconds=1.0, max_seconds=6.0,
                           silence_hold_seconds=0.4)
    segmenter.start(datetime.now(timezone.utc))
    try:
        segmenter.submit(_speech(1.5))
        segmenter.submit(_room_tone(1.0))      # a one-second pause
        segmenter.submit(_speech(1.0))
        _wait_for(collected)
        assert collected, "a one-second pause should have closed the clip"

        duration = len(collected[0].pcm) / BYTES_PER_SECOND
        # Speech ends at 1.5 s and resumes at 2.5 s. A cut inside the pause -
        # not before it, not after the next word has begun.
        assert 1.5 < duration < 2.5, f"cut at {duration:.2f}s, outside the pause"
    finally:
        segmenter.stop(seal_remaining=False)


def test_a_fricative_is_not_mistaken_for_a_pause():
    """An s sound is quiet. Cutting there would split the word that owns it."""
    collected = []
    segmenter = _segmenter(collected, min_seconds=0.5, max_seconds=6.0,
                           silence_hold_seconds=0.3)
    segmenter.start(datetime.now(timezone.utc))
    try:
        segmenter.submit(_speech(0.8))
        segmenter.submit(_fricative(0.6))      # longer than the hold, but speech
        segmenter.submit(_speech(0.8))
        time.sleep(0.6)
        assert not collected, "cut during a fricative - that splits a word"
    finally:
        segmenter.stop(seal_remaining=False)


def test_room_noise_does_not_stop_the_segmenter_finding_pauses():
    """
    A fixed threshold set for a quiet room never fires in a noisy one, so every
    clip is forced at the ceiling and every cut is arbitrary. The threshold
    follows the room instead.
    """
    collected = []
    segmenter = _segmenter(collected, min_seconds=1.0, max_seconds=8.0,
                           silence_hold_seconds=0.4, silence_rms=60)
    segmenter.start(datetime.now(timezone.utc))
    try:
        segmenter.submit(_room_tone(1.0, level=900))   # noisy background
        segmenter.submit(_speech(1.0))
        segmenter.submit(_room_tone(1.2, level=900))   # a pause, still noisy
        segmenter.submit(_speech(0.5))
        _wait_for(collected)
        assert collected, "a pause in a noisy room is still a pause"
    finally:
        segmenter.stop(seal_remaining=False)


def test_unbroken_speech_never_teaches_the_floor_that_speech_is_silence():
    """
    The floor is learned from the quietest moment in the recent past. A doctor
    who talks for a minute without pausing offers no quiet moments at all, and
    a naive estimator would settle on the speech itself - after which every
    frame reads as silence and the segmenter cuts wherever it likes, inside
    words.
    """
    collected = []
    segmenter = _segmenter(collected, min_seconds=1.0, max_seconds=30.0,
                           silence_hold_seconds=0.4)
    segmenter.start(datetime.now(timezone.utc))
    try:
        for _ in range(12):
            segmenter.submit(_speech(1.0))       # twelve seconds, no gap
        time.sleep(0.8)
        assert not collected, "cut during unbroken speech - that splits a word"
        # The floor may have crept up, but nowhere near speech.
        assert segmenter.noise_floor < 12000 * 0.3, segmenter.noise_floor
    finally:
        segmenter.stop(seal_remaining=False)


def test_a_pause_after_long_speech_is_still_found():
    """The protection above must not blind the segmenter to a real pause."""
    collected = []
    segmenter = _segmenter(collected, min_seconds=1.0, max_seconds=30.0,
                           silence_hold_seconds=0.4)
    segmenter.start(datetime.now(timezone.utc))
    try:
        for _ in range(8):
            segmenter.submit(_speech(1.0))
        segmenter.submit(_room_tone(1.0))
        segmenter.submit(_speech(0.5))
        _wait_for(collected)
        assert collected, "a pause after long speech is still a pause"
        duration = len(collected[0].pcm) / BYTES_PER_SECOND
        assert 8.0 < duration < 9.0, f"cut at {duration:.2f}s, outside the pause"
    finally:
        segmenter.stop(seal_remaining=False)


def _varying_speech(seconds: float, base: int = 9000, seed: int = 7) -> bytes:
    """
    Speech with the amplitude variation real speech has.

    Syllables, stresses and unstressed vowels swing far below the average. A
    threshold that drifts up towards the voice reads those dips as silence, and
    the clip is cut in the middle of a sentence - which is what happened.
    """
    import random
    rng = random.Random(seed)
    total = int(seconds * SAMPLE_RATE)
    out = array("h")
    while len(out) < total:
        syllable = int(rng.uniform(0.08, 0.25) * SAMPLE_RATE)
        level = int(base * rng.uniform(0.35, 1.0))
        for k in range(min(syllable, total - len(out))):
            out.append(level if (k % 9) < 4 else -level)
    return out.tobytes()


def test_ordinary_speech_is_not_cut_at_the_minimum():
    """
    Breaths between sentences are not gaps. With nothing longer than about a
    second anywhere, the clip must run on rather than cut the moment it becomes
    eligible.
    """
    collected = []
    segmenter = _segmenter(collected, min_seconds=3.0, max_seconds=30.0,
                           grace_seconds=5.0, silence_hold_seconds=3.0)
    segmenter.start(datetime.now(timezone.utc))
    try:
        for _ in range(4):
            segmenter.submit(_varying_speech(2.0))
            segmenter.submit(_room_tone(0.8))      # a breath, not a gap
        time.sleep(1.0)
        for clip in collected:
            duration = len(clip.pcm) / BYTES_PER_SECOND
            assert duration > 4.0, f"cut at {duration:.1f}s - at the minimum, mid-sentence"
    finally:
        segmenter.stop(seal_remaining=False)


def test_a_strong_gap_inside_the_window_is_taken():
    """Three seconds of quiet is a real break, and the clip should end there."""
    collected = []
    segmenter = _segmenter(collected, min_seconds=3.0, max_seconds=30.0,
                           grace_seconds=5.0, silence_hold_seconds=3.0)
    segmenter.start(datetime.now(timezone.utc))
    try:
        segmenter.submit(_varying_speech(5.0))
        segmenter.submit(_room_tone(3.5))          # a genuine break
        segmenter.submit(_varying_speech(2.0))
        _wait_for(collected)
        assert collected, "a three-second break should have closed the clip"
        duration = len(collected[0].pcm) / BYTES_PER_SECOND
        assert 5.0 < duration < 8.5, f"cut at {duration:.1f}s, outside the break"
    finally:
        segmenter.stop(seal_remaining=False)


def test_past_the_maximum_a_shorter_gap_will_do():
    """
    Overdue, the standard relaxes: rather than force a cut at the ceiling, a
    gap of half the usual length is accepted.
    """
    collected = []
    segmenter = _segmenter(collected, min_seconds=2.0, max_seconds=4.0,
                           grace_seconds=6.0, silence_hold_seconds=3.0)
    segmenter.start(datetime.now(timezone.utc))
    try:
        segmenter.submit(_varying_speech(5.0))     # already past the maximum
        segmenter.submit(_room_tone(1.8))          # too short before, enough now
        segmenter.submit(_varying_speech(1.0))
        _wait_for(collected)
        assert collected, "past the maximum a shorter gap should be taken"
        duration = len(collected[0].pcm) / BYTES_PER_SECOND
        assert duration < 9.0, f"ran to the ceiling at {duration:.1f}s"
    finally:
        segmenter.stop(seal_remaining=False)


# ============================================================
# The recording itself is never touched
#
# Everything the segmenter measures - loudness, zero crossings, the noise floor -
# is used to decide *where* to cut and for nothing else. No filtering, no noise
# reduction, no gating, no normalisation. The clips are the microphone's bytes,
# and joining them back gives the original recording exactly.
# ============================================================

def test_the_clips_reassemble_into_the_original_recording_byte_for_byte():
    """
    The strongest guarantee available: audio in, audio out, unchanged.

    A signal of random samples is used because every sample is distinct, so any
    alteration, loss, duplication or reordering changes the hash. This also
    covers the cut mechanics - a clip may end part-way through the buffer, and
    the remainder has to carry into the next clip with nothing dropped and
    nothing repeated.
    """
    import hashlib
    import random

    rng = random.Random(1234)
    source = array("h", [rng.randint(-20000, 20000)
                         for _ in range(SAMPLE_RATE * 12)]).tobytes()

    collected = []
    segmenter = _segmenter(collected, min_seconds=2.0, max_seconds=4.0,
                           grace_seconds=1.0, silence_hold_seconds=1.0)
    segmenter.start(datetime.now(timezone.utc))
    try:
        offset = fed = 0
        while offset < len(source):
            step = rng.choice([1764, 2048, 4096, 8192])
            segmenter.submit(source[offset:offset + step])
            offset += step
            fed += 1
            if fed % 40 == 0:
                time.sleep(0.05)      # the worker drains; real time would do this
        time.sleep(1.0)
    finally:
        segmenter.stop(seal_remaining=True)
    time.sleep(0.5)

    rebuilt = b"".join(clip.pcm for clip in collected)
    assert len(rebuilt) == len(source), (
        f"{len(source) - len(rebuilt)} bytes of the recording went missing")
    assert hashlib.sha256(rebuilt).digest() == hashlib.sha256(source).digest(), \
        "the audio that came out is not the audio that went in"
