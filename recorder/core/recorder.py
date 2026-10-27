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
* A microphone that goes mid-consultation - unplugged, or a driver that stops
  delivering - is replaced by the Windows default, which is the PC's own
  microphone once a USB one has gone. The recording carries on, and the
  seconds lost are reported.
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

    # ~95 s of audio at 44.1 kHz with 2048-frame buffers, about 8 MB. Deep enough
    # that a disk or GC stall cannot cost frames, shallow enough to bound memory.
    # Not read anywhere: `Segmenter.queue_depth` is the value that applies. Kept
    # in step with it so this does not document a depth the system does not use.
    QUEUE_DEPTH = 2048

    # How often the watchdog looks, and how often it tries again for a
    # microphone when none can be opened.
    WATCH_INTERVAL = 0.5
    REOPEN_INTERVAL = 1.0
    # How often, while recording on another microphone, to look for the
    # clinic's own (the Jabra) having been plugged back in.
    PREFERRED_CHECK_SECONDS = 5.0

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
        on_device_change: Optional[Callable[[str, float], None]] = None,
        stall_seconds: float = 2.5,
        preferred_name: str = "",
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self.frames_per_buffer = frames_per_buffer
        self.input_device_index = input_device_index

        self._on_chunk = on_chunk
        self._on_error = on_error
        self._on_device_change = on_device_change
        self.stall_seconds = stall_seconds
        # Part of the clinic microphone's name ("Jabra"). Opened by name, so it
        # records whether or not Windows has made it the default.
        self.preferred_name = preferred_name

        self._pyaudio = None
        self._stream = None
        self._thread: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._state_lock = threading.RLock()
        # Each stream gets its own capture thread, numbered: a thread whose
        # stream has been replaced sees a newer number and stops.
        self._generation = 0
        self._failed = False
        self._last_read = 0.0
        self._abandoned: list = []
        self.device_name = "microphone"

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
        with self._state_lock:
            if self._running.is_set():
                logger.warning("Capture already running")
                return
            self.stats = CaptureStats(started_at=datetime.now(timezone.utc))
            self._open(self.input_device_index)
            self._running.set()
            self._launch()
            self._watchdog = threading.Thread(
                target=self._watch, name="AudioWatchdog", daemon=True)
            self._watchdog.start()
            logger.info("Capture started: %s Hz, %s ch, %s-bit, device=%s",
                        self.sample_rate, self.channels, self.sample_width * 8,
                        self.device_name)

    def stop(self) -> CaptureStats:
        """Ask the capture loop to finish, wait for it, then release the device."""
        with self._state_lock:
            if not self._running.is_set():
                return self.stats
            self._running.clear()

        # The watchdog first: once it has gone, nothing swaps the stream under us.
        watchdog = self._watchdog
        if watchdog and watchdog.is_alive() and watchdog is not threading.current_thread():
            watchdog.join(timeout=5.0)

        thread = self._thread
        if thread and thread.is_alive():
            # Generous relative to one buffer period; the loop exits after at most
            # one blocking read.
            thread.join(timeout=5.0)
        if thread and thread.is_alive():
            # Blocked in a read on a device that has gone. Closing the stream under
            # it can crash PortAudio, so it is left to the operating system.
            logger.error("Capture thread did not exit; leaving its device to Windows")
            self._abandon()
        else:
            self._teardown_stream()
        self.stats.stopped_at = datetime.now(timezone.utc)
        logger.info("Capture stopped: %.1f s, %s overruns, %s read errors, %s device change(s)",
                    self.duration_seconds, self.stats.overruns, self.stats.read_errors,
                    self.stats.device_changes)
        return self.stats

    def _open(self, index: Optional[int]) -> None:
        """
        Open `index`, or the Windows default microphone if that fails. A fresh
        PortAudio handle every time: one created earlier still lists a device
        that has since been unplugged, and not the one that replaced it.
        """
        import pyaudio

        handle = pyaudio.PyAudio()
        last: Optional[Exception] = None
        if index is not None:
            candidates = [index, None]
        else:
            candidates = [self._find(handle, self.preferred_name), None]
            candidates = [c for n, c in enumerate(candidates) if n == 1 or c is not None]
        for candidate in candidates:
            try:
                stream = handle.open(
                    format=pyaudio.paInt16 if self.sample_width == 2 else pyaudio.paInt32,
                    channels=self.channels,
                    rate=self.sample_rate,
                    input=True,
                    input_device_index=candidate,
                    frames_per_buffer=self.frames_per_buffer,
                )
            except Exception as exc:
                last = exc
                continue
            if index is not None and candidate != index:
                logger.warning("Microphone %s could not be opened (%s); using the "
                               "Windows default instead", index, last)
            self._pyaudio, self._stream = handle, stream
            self.device_name = self._name_of(handle, candidate)
            return
        try:
            handle.terminate()
        except Exception:
            pass
        raise AudioCaptureError(f"cannot open input device: {last}")

    @staticmethod
    def _find(handle, name: str) -> Optional[int]:
        """
        The input device that is `name` ("Jabra"), on the default host API.

        That API (MME) cuts device names to 31 characters, so the Jabra Speak2
        55 is listed as "Echo Cancelling Speakerphone (J" and the word Jabra is
        gone. Each listed name is therefore matched against Windows' full names
        too (core.microphone): the cut name is the start of the full one.
        """
        if not name:
            return None
        from core.microphone import endpoint_names, same_device
        wanted = [full for full in endpoint_names() if name.lower() in full.lower()]
        try:
            host = handle.get_default_host_api_info()["index"]
        except Exception:
            host = None
        try:
            count = handle.get_device_count()
        except Exception:
            return None
        for i in range(count):
            try:
                info = handle.get_device_info_by_index(i)
            except Exception:
                continue
            listed = str(info.get("name", ""))
            if (int(info.get("maxInputChannels", 0)) > 0
                    and (host is None or info.get("hostApi") == host)
                    and (name.lower() in listed.lower()
                         or any(same_device(listed, full) for full in wanted))):
                return i
        return None

    @staticmethod
    def _name_of(handle, index: Optional[int]) -> str:
        """The device's full Windows name where it can be found, else the listed one."""
        try:
            info = (handle.get_default_input_device_info() if index is None
                    else handle.get_device_info_by_index(index))
            listed = str(info.get("name", "microphone"))
        except Exception:
            return "microphone"
        from core.microphone import endpoint_names, same_device
        return next((full for full in endpoint_names() if same_device(listed, full)), listed)

    def _launch(self) -> None:
        """A capture thread for the current stream. Called with the state lock held."""
        self._generation += 1
        self._failed = False
        self._last_read = time.monotonic()
        self._thread = threading.Thread(
            target=self._capture_loop, args=(self._generation, self._stream),
            name="AudioCapture", daemon=True)
        self._thread.start()

    def _abandon(self) -> None:
        """
        Let go of a stream whose reader is stuck, without closing it. Kept
        referenced so the garbage collector cannot close it under that reader.
        """
        if self._stream is not None:
            self._abandoned.append((self._stream, self._pyaudio, self._thread))
        self._stream, self._pyaudio = None, None

    def _reap_abandoned(self) -> None:
        """
        Close abandoned streams whose stuck reader has finally returned. Without
        this every hiccup of a Bluetooth headset left a device handle open for
        the rest of the day.
        """
        keep = []
        for stream, handle, thread in self._abandoned:
            if thread is not None and thread.is_alive():
                keep.append((stream, handle, thread))
                continue
            for close in (getattr(stream, "close", None), getattr(handle, "terminate", None)):
                try:
                    if close is not None:
                        close()
                except Exception as exc:
                    logger.debug("Closing an abandoned stream: %s", exc)
        self._abandoned = keep

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

    def _capture_loop(self, generation: int, stream) -> None:
        chunk_frames = self.frames_per_buffer
        consecutive_errors = 0

        while self._running.is_set() and generation == self._generation:
            try:
                data = stream.read(chunk_frames, exception_on_overflow=False)
                consecutive_errors = 0
            except Exception as exc:
                # A read in flight when stop() closes the device fails, and so
                # does every read after it. Those are the shutdown itself, not a
                # fault: counting them reported "20 read errors" on a clean stop
                # and put an alarming message in front of the doctor.
                if not self._running.is_set() or generation != self._generation:
                    break

                self.stats.read_errors += 1
                consecutive_errors += 1
                logger.error("Audio read failed (%s in a row): %s", consecutive_errors, exc)
                # A handful of transient failures happen when a device is switched.
                # A sustained run means the device is gone: the watchdog moves the
                # recording to whichever microphone Windows now offers.
                if consecutive_errors >= 20:
                    self._failed = True
                    break
                time.sleep(0.05)
                continue

            if generation != self._generation:
                break                   # replaced while this read was blocked
            self._last_read = time.monotonic()
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

    # ---- failover ----

    def _watch(self) -> None:
        """
        Notice a microphone that has stopped delivering, and carry on with another.

        A microphone goes one of two ways: reads fail (the loop gives up after
        twenty), or - what Windows usually does when a USB microphone is pulled
        out - a read simply never returns. Either way the consultation must not
        go on "recording" nothing, so the stream is replaced by the Windows
        default, which is the PC's own microphone once the USB one has gone.
        """
        checked = time.monotonic()
        while self._running.is_set():
            time.sleep(self.WATCH_INTERVAL)
            if not self._running.is_set():
                return
            if self._abandoned:
                with self._state_lock:
                    self._reap_abandoned()
            stalled = time.monotonic() - self._last_read > self.stall_seconds
            if self._failed or stalled:
                self._failover("reads failed" if self._failed else
                               f"no audio for {self.stall_seconds:.1f}s")
            elif time.monotonic() - checked >= self.PREFERRED_CHECK_SECONDS:
                checked = time.monotonic()
                self._return_to_preferred()

    def on_preferred(self) -> bool:
        """Whether the recording is on the clinic's own microphone (or none is named)."""
        return not self.preferred_name or \
            self.preferred_name.lower() in (self.device_name or "").lower()

    def _return_to_preferred(self) -> None:
        """
        The Jabra was unplugged and the recording carried on with the PC's own
        microphone. Once it is plugged back in, the recording moves back to it:
        it is opened first and only then swapped in, so at most one buffer of
        audio is between the two. Nothing happens while it stays away.
        """
        if self.on_preferred():
            return
        from core.microphone import endpoint_names
        if not any(self.preferred_name.lower() in name.lower() for name in endpoint_names()):
            return                              # still unplugged: the cheap check, every time
        import pyaudio
        handle = pyaudio.PyAudio()
        stream = None
        try:
            index = self._find(handle, self.preferred_name)
            if index is None:
                handle.terminate()
                return
            stream = handle.open(
                format=pyaudio.paInt16 if self.sample_width == 2 else pyaudio.paInt32,
                channels=self.channels, rate=self.sample_rate, input=True,
                input_device_index=index, frames_per_buffer=self.frames_per_buffer)
        except Exception as exc:
            logger.info("The clinic microphone is back but could not be opened yet: %s", exc)
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            handle.terminate()
            return
        with self._state_lock:
            if not self._running.is_set():
                stream.close()
                handle.terminate()
                return
            self._abandon()                     # the old reader sees a newer generation and stops
            self._pyaudio, self._stream = handle, stream
            self.device_name = self._name_of(handle, index)
            self.stats.device_changes += 1
            self._launch()
        logger.warning("The clinic microphone is back: recording continues on %s",
                       self.device_name)
        if self._on_device_change:
            self._on_device_change(self.device_name, 0.0)

    def _failover(self, why: str) -> None:
        lost_at = self._last_read
        logger.critical("Microphone stopped (%s); switching to the default microphone", why)
        with self._state_lock:
            if not self._running.is_set():
                return
            thread = self._thread
            if thread is not None and thread.is_alive():
                self._abandon()             # stuck in a read: do not touch it
            else:
                self._teardown_stream()
            self._generation += 1           # whatever that reader returns is discarded

        told = False
        while True:
            with self._state_lock:
                if not self._running.is_set():
                    return
                try:
                    self._open(None)
                except AudioCaptureError as exc:
                    error = exc
                else:
                    self.stats.device_changes += 1
                    self._launch()
                    break
            if not told and self._on_error:
                self._on_error(f"the microphone stopped and no other one is available: {error}")
                told = True
            time.sleep(self.REOPEN_INTERVAL)

        gap = time.monotonic() - lost_at
        logger.warning("Recording continues on %s after %.1fs without audio",
                       self.device_name, gap)
        if self._on_device_change:
            self._on_device_change(self.device_name, gap)

    # ---- metrics ----

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * self.sample_width

    @property
    def duration_seconds(self) -> float:
        """Duration derived from captured bytes, so it never drifts from the audio."""
        return self.stats.bytes_captured / max(1, self.bytes_per_second)
