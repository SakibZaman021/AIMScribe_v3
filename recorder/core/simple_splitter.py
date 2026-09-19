"""
Segmenter - cuts the audio stream into clips that upload as they are produced.

A clip closes on a silence boundary once past the minimum length, or is forced at
the maximum. Between those two points the segmenter watches for a sustained quiet
patch so cuts land in a natural gap rather than mid-sentence.

The important structural change from v1: this runs on **its own thread**, fed by a
queue. Previously `process_chunk` ran on the PyAudio callback thread and wrote up
to 12 MB of WAV synchronously inside it, which dropped frames at every clip
boundary. Now the capture thread only enqueues; loudness maths and sealing happen
here.

Commands (flush, pause, stop) travel through the same queue as audio, so they are
always processed after every chunk that preceded them. Ordering is what makes a
pause boundary exact.
"""
from __future__ import annotations

import logging
import queue
import threading
from array import array
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# Prefer the C implementation where it exists. `audioop` is deprecated in 3.11 and
# removed in 3.13, so the pure-Python fallback is not optional.
try:  # pragma: no cover - depends on interpreter version
    import audioop  # type: ignore[import-not-found]

    def _rms(fragment: bytes, width: int) -> float:
        return float(audioop.rms(fragment, width))

except ImportError:  # pragma: no cover
    def _rms(fragment: bytes, width: int) -> float:
        if width != 2 or not fragment:
            return 0.0
        # ~150 µs per 2048-sample chunk, about 0.3% of one core at 44.1 kHz.
        samples = array("h")
        samples.frombytes(fragment[: len(fragment) - (len(fragment) % 2)])
        if not samples:
            return 0.0
        total = 0
        for value in samples:
            total += value * value
        return (total / len(samples)) ** 0.5


def _zero_crossing_rate(fragment: bytes) -> float:
    """
    How often the waveform changes sign, per sample.

    Unvoiced fricatives - the s, sh, f and th sounds - carry little energy but
    cross zero constantly. Judging on loudness alone, they look like silence,
    which is exactly how a cut lands in the middle of "diabetes".
    """
    samples = array("h")
    samples.frombytes(fragment[: len(fragment) - (len(fragment) % 2)])
    if len(samples) < 2:
        return 0.0
    crossings = 0
    previous = samples[0]
    for value in samples:
        if (value >= 0) != (previous >= 0):
            crossings += 1
        previous = value
    return crossings / (len(samples) - 1)


@dataclass(frozen=True)
class SealedSegment:
    """What the segmenter hands back for spooling."""
    pcm: bytes
    captured_start_at: datetime
    captured_end_at: datetime
    rms_mean: float
    is_final: bool


class _Command:
    """Queue sentinel so commands are ordered against the audio that precedes them."""
    __slots__ = ("kind", "done")

    def __init__(self, kind: str):
        self.kind = kind
        self.done = threading.Event()


class Segmenter:
    """
    Consumes raw PCM chunks and emits sealed segments.

    `on_segment` is called on the segmenter thread. It is expected to do the disk
    write (Spool.seal_segment) and return; uploading happens elsewhere.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        sample_width: int,
        min_seconds: float,
        max_seconds: float,
        grace_seconds: float = 0.0,
        silence_rms: int,
        silence_hold_seconds: float,
        on_segment: Callable[[SealedSegment], None],
        queue_depth: int = 512,
    ):
        self.bytes_per_second = sample_rate * channels * sample_width
        self.sample_width = sample_width
        self.min_bytes = int(min_seconds * self.bytes_per_second)
        self.max_bytes = int(max_seconds * self.bytes_per_second)
        # The absolute ceiling. Between max and this, the clip is over its
        # target length but still being given a chance to end on a quiet patch
        # rather than in the middle of a word.
        self.hard_bytes = int((max_seconds + grace_seconds) * self.bytes_per_second)
        # `silence_rms` is now a floor rather than the whole test. A fixed
        # threshold cannot work across a quiet consulting room and one with a
        # fan and a corridor outside: set low, the noisy room never cuts and
        # every clip is forced mid-word; set high, the quiet room cuts between
        # syllables. The live threshold tracks the room and never drops below
        # this.
        self.silence_rms = silence_rms
        # A strong gap: three seconds of quiet, far longer than a breath between
        # sentences, so a cut here cannot land inside a phrase. This is what is
        # looked for between the minimum and the maximum.
        self.silence_hold_bytes = int(silence_hold_seconds * self.bytes_per_second)
        # Past the maximum the clip is overdue and the standard relaxes: half a
        # strong gap will do. Better a cut at a one-and-a-half second pause than
        # one forced at the ceiling wherever the words happen to be.
        self.relaxed_hold_bytes = max(1, self.silence_hold_bytes // 2)
        self._on_segment = on_segment

        # 20 ms is short enough to place a cut precisely inside a gap, long
        # enough for the loudness and zero-crossing figures to mean something.
        self.frame_bytes = max(1, int(0.020 * self.bytes_per_second))
        self.frame_bytes -= self.frame_bytes % max(1, sample_width)
        # A pause between words runs 50-200 ms; between sentences, 300-800 ms.
        # Anything shorter than the hold is speech taking a breath.
        self.noise_floor = float(silence_rms)
        # The floor is learned by watching for the quietest moment in each short
        # window and keeping the smallest of the last few. Learning only from
        # frames already judged quiet cannot work: in a room noisier than the
        # starting guess, nothing is ever judged quiet, so the estimate never
        # rises and every clip is forced at the ceiling.
        self._window_bytes = int(0.5 * self.bytes_per_second)
        self._window_seen = 0
        self._window_min = float("inf")
        self._recent_minima: list = []
        # An exponential average of speech, used to cap the floor. Without it,
        # a doctor who never pauses would teach the floor that speech is the
        # background, and the segmenter would start cutting inside words.
        self._speech_level = 0.0
        # Fricatives sit well above the room but well below a vowel, so a
        # threshold a few times the floor keeps them on the speech side.
        self.silence_ratio = 3.0
        self.fricative_zcr = 0.18
        # When a cut is forced, look back this far for the quietest moment
        # rather than slicing wherever the clip happened to reach.
        self.lookback_bytes = int(5.0 * self.bytes_per_second)

        self._queue: "queue.Queue" = queue.Queue(maxsize=queue_depth)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # Segment being assembled. Byte counts are integers so duration never drifts.
        self._buffer = bytearray()
        self._silence_bytes = 0
        self._rms_weighted_sum = 0.0
        self._rms_samples = 0
        self._segment_started_at: Optional[datetime] = None
        # (end_offset, rms, is_speech) for every 20 ms of the clip so far, and
        # how much of the buffer has been analysed.
        self._frames: list = []
        self._framed_bytes = 0
        self._silence_started_at_offset: Optional[int] = None

        self.segments_emitted = 0
        self.dropped_chunks = 0

    # ---- lifecycle ----

    def start(self, started_at: Optional[datetime] = None) -> None:
        self._segment_started_at = started_at or datetime.now(timezone.utc)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="Segmenter", daemon=True)
        self._thread.start()

    def set_segment_start(self, when: datetime) -> None:
        """
        Move the current segment's start timestamp.

        Used on resume: without it the next segment would be stamped as beginning
        when the previous one ended, implying continuous audio across a pause.
        """
        self._segment_started_at = when

    def submit(self, chunk: bytes) -> None:
        """
        Called from the capture thread. Raises queue.Full rather than blocking.

        AudioRecorder counts the Full case as an overrun; blocking here would stall
        PortAudio and lose the same audio with worse timing.
        """
        self._queue.put_nowait(chunk)

    def flush(self, *, is_final: bool = False, timeout: float = 30.0) -> bool:
        """
        Seal whatever is buffered. Used at pause and at stop.

        Blocks until the segmenter has drained every chunk queued before this call,
        so a pause boundary is exact rather than approximate.
        """
        command = _Command("final" if is_final else "flush")
        try:
            self._queue.put(command, timeout=5.0)
        except queue.Full:
            logger.error("Could not enqueue flush: segmenter queue is full")
            return False
        return command.done.wait(timeout=timeout)

    def stop(self, *, seal_remaining: bool = True, timeout: float = 30.0) -> None:
        if self._thread is None:
            return
        if seal_remaining:
            self.flush(is_final=True, timeout=timeout)
        self._stop.set()
        try:
            self._queue.put_nowait(_Command("stop"))
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)
        self._thread = None

    # ---- worker ----

    def _run(self) -> None:
        logger.debug("Segmenter thread started")
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue

            if isinstance(item, _Command):
                if item.kind in ("flush", "final"):
                    self._seal(is_final=(item.kind == "final"))
                    item.done.set()
                    continue
                if item.kind == "stop":
                    item.done.set()
                    break

            self._consume(item)

        logger.debug("Segmenter thread finished after %s segment(s)", self.segments_emitted)

    def _consume(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

        level = _rms(chunk, self.sample_width)
        self._rms_weighted_sum += level * len(chunk)
        self._rms_samples += len(chunk)

        self._analyse_new_frames()
        size = len(self._buffer)

        # Below the minimum, keep accumulating regardless of how quiet it is.
        # Otherwise a cough in the first seconds produces a one-second clip.
        if size < self.min_bytes:
            # Below the minimum nothing is cut, but the pause is still tracked:
            # a gap that begins at 28 s and runs to 33 s is a real gap, and
            # forgetting it would throw away the best cut in the clip.
            return

        # A sustained pause, and past the minimum: cut in the middle of it, so
        # neither clip begins or ends against a word. Between the minimum and
        # the maximum only a strong gap qualifies; past the maximum, where the
        # clip is already overdue, a shorter one is accepted.
        required = (self.relaxed_hold_bytes if size >= self.max_bytes
                    else self.silence_hold_bytes)
        if (self._silence_bytes >= required
                and self._silence_started_at_offset is not None):
            middle = self._silence_started_at_offset + self._silence_bytes // 2
            logger.debug("Pause at %.1f s; cutting inside it at %.1f s",
                         size / self.bytes_per_second, middle / self.bytes_per_second)
            self._seal(is_final=False, cut_at=middle)
            return

        # Out of grace. The clip has to end, but not just anywhere: the quietest
        # moment in the last couple of seconds is far more likely to be a gap
        # between words than the arbitrary point the ceiling falls on.
        if size >= self.hard_bytes:
            cut = self._quietest_offset_near_end()
            logger.debug("Segment reached its ceiling at %.1f s; cutting at the "
                         "quietest point, %.1f s",
                         size / self.bytes_per_second,
                         (cut or size) / self.bytes_per_second)
            self._seal(is_final=False, cut_at=cut)

    # ---- listening ----

    def _analyse_new_frames(self) -> None:
        """
        Walk the newly arrived audio one 20 ms frame at a time.

        Chunks do not arrive in tidy multiples, so this consumes whole frames
        and leaves the remainder for the next chunk.
        """
        while len(self._buffer) - self._framed_bytes >= self.frame_bytes:
            start = self._framed_bytes
            frame = bytes(self._buffer[start:start + self.frame_bytes])
            self._framed_bytes += self.frame_bytes

            level = _rms(frame, self.sample_width)

            # Judge against the floor as it stands, then let the result decide
            # how the floor moves. Updating first let speech drag the estimate
            # up - fifty frames a second, so "slowly" was nothing of the sort -
            # until the threshold sat above a fricative and the guard below
            # stopped firing.
            threshold = max(float(self.silence_rms), self.noise_floor * self.silence_ratio)
            # A hard ceiling on the threshold as well as on the floor. Silence
            # is silence: it does not creep up towards the volume of the voice,
            # whatever the room is doing.
            if self._speech_level:
                threshold = min(threshold, self._speech_level * 0.20)
            quiet = level < threshold

            # Quiet by loudness, but crossing zero on nearly every sample: an
            # unvoiced consonant, not a gap. Cutting here splits the word that
            # owns it. Judged against the absolute floor rather than the running
            # estimate, so it holds even when the room is loud.
            if quiet and level > self.silence_rms and                     _zero_crossing_rate(frame) >= self.fricative_zcr:
                quiet = False

            if not quiet:
                self._speech_level = (0.95 * self._speech_level + 0.05 * level
                                      if self._speech_level else level)

            self._window_min = min(self._window_min, level)
            self._window_seen += self.frame_bytes
            if self._window_seen >= self._window_bytes:
                self._recent_minima.append(self._window_min)
                del self._recent_minima[:-4]        # about two seconds of history
                self._window_seen = 0
                self._window_min = float("inf")

                # The quietest moment in the last two seconds is the room. Two
                # seconds of history means one window of unbroken speech cannot
                # move it - a real pause has to be absent throughout.
                candidate = min(self._recent_minima)
                # ...and even then, the floor stays far below speech. At a
                # quarter of the speech level, three times the floor put the
                # threshold at 75% of speech: every dip between syllables read
                # as silence, a second of it piled up mid-sentence, and the clip
                # cut the moment it passed the minimum. Which is exactly what
                # was heard.
                if self._speech_level:
                    candidate = min(candidate, self._speech_level * 0.06)
                self.noise_floor = max(float(self.silence_rms), candidate)

            self._frames.append((self._framed_bytes, level, not quiet))

            if quiet:
                if self._silence_started_at_offset is None:
                    self._silence_started_at_offset = start
                self._silence_bytes += self.frame_bytes
            else:
                self._silence_bytes = 0
                self._silence_started_at_offset = None

    def _quietest_offset_near_end(self) -> Optional[int]:
        """
        The end of the quietest frame in the recent past, if there is a real dip.

        Used only when a cut is forced. Backing off to a quieter moment is worth
        a little clip length, but only when that moment is genuinely quieter:
        against steady speech every frame measures much the same, and picking
        the "quietest" of those would just cut early for no benefit - and, with
        ties, as early as the window allows.

        Returns None when the recent audio is uniformly loud, and the caller
        then cuts at the ceiling as before.
        """
        earliest = len(self._buffer) - self.lookback_bytes
        window = [(offset, rms) for offset, rms, _ in self._frames
                  if offset >= earliest and offset >= self.min_bytes]
        if len(window) < 3:
            return None

        levels = sorted(rms for _, rms in window)
        median = levels[len(levels) // 2]
        quietest = levels[0]
        if quietest > median * 0.6:
            return None                       # no real dip to aim for

        # Among the dips, take the latest: it keeps the clip closest to its
        # target length, and a later gap is as good a place to cut as an
        # earlier one.
        ceiling = quietest * 1.2
        return max(offset for offset, rms in window if rms <= ceiling)

    def _seal(self, *, is_final: bool, cut_at: Optional[int] = None) -> None:
        """
        Emit the clip, optionally cutting at a chosen point.

        `cut_at` is a byte offset inside the buffer. Everything after it stays
        and becomes the beginning of the next clip, which is what allows a cut
        to land in the middle of a pause instead of at whatever moment the
        buffer happened to reach.
        """
        if not self._buffer:
            # A flush with nothing buffered is normal (pause immediately after a cut).
            if is_final:
                logger.debug("Final flush with an empty buffer; nothing to seal")
            return

        # Whole samples only: cutting mid-sample would put a click at the join.
        if cut_at is not None:
            cut_at -= cut_at % self.sample_width
            cut_at = max(self.sample_width, min(cut_at, len(self._buffer)))
        split = cut_at if cut_at is not None else len(self._buffer)

        pcm = bytes(self._buffer[:split])
        remainder = bytearray(self._buffer[split:])
        started = self._segment_started_at or datetime.now(timezone.utc)
        ended = started + timedelta(seconds=len(pcm) / self.bytes_per_second)
        mean_rms = (self._rms_weighted_sum / self._rms_samples) if self._rms_samples else 0.0

        # Reset before the callback: sealing writes to disk and could raise, and a
        # retry must not re-emit audio that is already accounted for in the chain.
        self._buffer = remainder
        self._silence_bytes = 0
        self._silence_started_at_offset = None
        self._frames = []
        self._framed_bytes = 0
        self._rms_weighted_sum = 0.0
        self._rms_samples = 0
        self._segment_started_at = ended

        try:
            self._on_segment(SealedSegment(
                pcm=pcm,
                captured_start_at=started,
                captured_end_at=ended,
                rms_mean=mean_rms,
                is_final=is_final,
            ))
            self.segments_emitted += 1
        except Exception as exc:
            # Losing a segment here means losing audio, so this is loud.
            logger.critical("Failed to seal segment: %s", exc, exc_info=True)
            raise

    # ---- metrics ----

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def buffered_seconds(self) -> float:
        return len(self._buffer) / self.bytes_per_second


__all__ = ["Segmenter", "SealedSegment"]
