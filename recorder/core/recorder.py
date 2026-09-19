"""
Audio capture.

Records WAV PCM from the default input device and hands raw chunks to a consumer
on another thread. The capture thread does nothing but read from PortAudio and
enqueue - no hashing, no loudness maths, no file writes - because anything slow
here shows up as dropped frames in the recording.

Changes from v1 that matter:

* No singleton. The old `get_instance`/`reset_instance` pair mutated a live
  object's `_initialized` flag while its capture thread was still running, and
  closed the PortAudio stream on a 2-second timeout regardless. One recorder is
  now owned by one session.
* The whole consultation is no longer accumulated in RAM. Memory is bounded by
  the queue plus the segment being assembled, so a three-hour session costs the
  same as a three-minute one.
* Stop is ordered: the capture loop is asked to finish, joined, and only then is
  the stream closed.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class CaptureStats:
    """Health counters surfaced in the heartbeat and in integrity alerts."""
    frames: int = 0
    bytes_captured: int = 0
    overruns: int = 0          # consumer fell behind; audio was dropped
    read_errors: int = 0       # PortAudio read failures
    device_changes: int = 0
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None

    def as_dict(self) -> dict:
        return {
            "frames": self.frames,
            "bytes_captured": self.bytes_captured,
            "overruns": self.overruns,
            "read_errors": self.read_errors,
            "device_changes": self.device_changes,
        }


@dataclass
class InputDevice:
    index: int
    name: str
    channels: int
    default_sample_rate: float


class AudioCaptureError(RuntimeError):
    """Raised when the input device cannot be opened."""


class AudioRecorder:
    """
    One PyAudio input stream feeding a consumer callback.

    The consumer must be cheap and non-blocking; `Segmenter.submit` is designed
    for exactly this and does a single queue put.
    """

    # ~23 s of audio at 44.1 kHz with 2048-frame buffers. Deep enough that a brief
    # disk or GC stall cannot cost frames, shallow enough to bound memory.
    QUEUE_DEPTH = 512

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        sample_width: int,
        frames_per_buffer: int,
        input_device_index: Optional[int] = None,
        on_chunk: Optional[Callable[[bytes], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self.frames_per_buffer = frames_per_buffer
        self.input_device_index = input_device_index

        self._on_chunk = on_chunk
        self._on_error = on_error

        self._pyaudio = None
        self._stream = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._state_lock = threading.RLock()

        self.stats = CaptureStats()

    # ---- device discovery ----

    @staticmethod
    def list_input_devices() -> List[InputDevice]:
        """Enumerate input devices, for the tray's device picker and for diagnostics."""
        import pyaudio

        found: List[InputDevice] = []
        handle = pyaudio.PyAudio()
        try:
            for index in range(handle.get_device_count()):
                try:
                    info = handle.get_device_info_by_index(index)
                except Exception:
                    continue
                if int(info.get("maxInputChannels", 0)) > 0:
                    found.append(InputDevice(
                        index=index,
                        name=str(info.get("name", f"device {index}")),
                        channels=int(info["maxInputChannels"]),
                        default_sample_rate=float(info.get("defaultSampleRate", 0.0)),
                    ))
        finally:
            handle.terminate()
        return found

    def describe_device(self) -> str:
        try:
            devices = self.list_input_devices()
        except Exception:
            return "unknown"
        if self.input_device_index is None:
            return devices[0].name if devices else "no input device"
        for device in devices:
            if device.index == self.input_device_index:
                return device.name
        return f"index {self.input_device_index} (not present)"

    # ---- lifecycle ----

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    def start(self) -> None:
        import pyaudio

        with self._state_lock:
            if self._running.is_set():
                logger.warning("Capture already running")
                return

            try:
                self._pyaudio = pyaudio.PyAudio()
                self._stream = self._pyaudio.open(
                    format=pyaudio.paInt16 if self.sample_width == 2 else pyaudio.paInt32,
                    channels=self.channels,
                    rate=self.sample_rate,
                    input=True,
                    input_device_index=self.input_device_index,
                    frames_per_buffer=self.frames_per_buffer,
                )
            except Exception as exc:
                self._teardown_stream()
                raise AudioCaptureError(f"cannot open input device: {exc}") from exc

            self.stats = CaptureStats(started_at=datetime.now(timezone.utc))
            self._running.set()
            self._thread = threading.Thread(
                target=self._capture_loop, name="AudioCapture", daemon=True)
            self._thread.start()
            logger.info("Capture started: %s Hz, %s ch, %s-bit, device=%s",
                        self.sample_rate, self.channels, self.sample_width * 8,
                        self.describe_device())

    def stop(self) -> CaptureStats:
        """Ask the capture loop to finish, wait for it, then release the device."""
        with self._state_lock:
            if not self._running.is_set():
                return self.stats
            self._running.clear()

        thread = self._thread
        if thread and thread.is_alive():
            # Generous relative to one buffer period; the loop exits after at most
            # one blocking read.
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.error("Capture thread did not exit; releasing the device anyway")

        self._teardown_stream()
        self.stats.stopped_at = datetime.now(timezone.utc)
        logger.info("Capture stopped: %.1f s, %s overruns, %s read errors",
                    self.duration_seconds, self.stats.overruns, self.stats.read_errors)
        return self.stats

    def _teardown_stream(self) -> None:
        stream, handle = self._stream, self._pyaudio
        self._stream, self._pyaudio = None, None
        try:
            if stream is not None:
                if stream.is_active():
                    stream.stop_stream()
                stream.close()
        except Exception as exc:
            logger.warning("Error closing audio stream: %s", exc)
        try:
            if handle is not None:
                handle.terminate()
        except Exception as exc:
            logger.warning("Error terminating PyAudio: %s", exc)

    # ---- capture ----

    def _capture_loop(self) -> None:
        stream = self._stream
        chunk_frames = self.frames_per_buffer
        consecutive_errors = 0

        while self._running.is_set():
            try:
                data = stream.read(chunk_frames, exception_on_overflow=False)
                consecutive_errors = 0
            except Exception as exc:
                # A read in flight when stop() closes the device fails, and so
                # does every read after it. Those are the shutdown itself, not a
                # fault: counting them reported "20 read errors" on a clean stop
                # and put an alarming message in front of the doctor.
                if not self._running.is_set():
                    break

                self.stats.read_errors += 1
                consecutive_errors += 1
                logger.error("Audio read failed (%s in a row): %s", consecutive_errors, exc)
                # A handful of transient failures happen when a device is switched.
                # A sustained run means the device is gone and recording is not
                # actually happening, which the caller must be told about.
                if consecutive_errors >= 20:
                    if self._on_error:
                        self._on_error(f"input device failed: {exc}")
                    break
                time.sleep(0.05)
                continue

            self.stats.frames += chunk_frames
            self.stats.bytes_captured += len(data)

            if self._on_chunk is None:
                continue
            try:
                self._on_chunk(data)
            except queue.Full:
                # Never block the capture thread: blocking here guarantees a
                # PortAudio overrun, which loses the same audio and corrupts timing.
                self.stats.overruns += 1
                if self.stats.overruns in (1, 10, 100) or self.stats.overruns % 1000 == 0:
                    logger.critical("Segmenter is not keeping up; %s chunk(s) dropped",
                                    self.stats.overruns)
            except Exception as exc:
                logger.error("Chunk consumer raised: %s", exc)

    # ---- metrics ----

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * self.sample_width

    @property
    def duration_seconds(self) -> float:
        """Duration derived from captured bytes, so it never drifts from the audio."""
        return self.stats.bytes_captured / max(1, self.bytes_per_second)
