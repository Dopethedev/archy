"""Microphone capture and speaker playback.

Both devices are opened through pipewire, which resamples for us: the headset
hardware only runs at 48 kHz, while whisper, silero and openWakeWord all want
16 kHz. Asking PortAudio for the raw hw device fails; the default pipewire
device accepts anything.
"""

from __future__ import annotations

import collections
import queue
import threading

import numpy as np
import sounddevice as sd

from . import config


def resolve_input(device):
    """Turn a device name into one device.

    A substring is convenient but often ambiguous: "Fifine" matches both the
    hardware device and its JACK alias, and sounddevice refuses to guess. So
    prefer an exact name, then the first candidate that actually opens at the
    rate we need, and only then give up.
    """
    if device is None or isinstance(device, int):
        return device

    needle = str(device).lower()
    candidates = [i for i, d in enumerate(sd.query_devices())
                  if needle in d["name"].lower() and d["max_input_channels"] > 0]
    if not candidates:
        raise ValueError(f"no input device matching {device!r}")
    if len(candidates) == 1:
        return candidates[0]

    for i in candidates:                                  # exact name wins
        if sd.query_devices(i)["name"].lower() == needle:
            return i
    for i in candidates:                                  # else first that opens
        try:
            sd.check_input_settings(device=i, samplerate=config.SAMPLE_RATE, channels=1)
            return i
        except Exception:
            continue
    return candidates[0]


class MicCapture:
    """16 kHz mono int16 frames, handed over a plain thread-safe queue."""

    def __init__(self, blocksize: int = config.INPUT_BLOCK, device=None):
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self.level = 0.0                     # 0..1, for the HUD waveform
        self._blocksize = blocksize
        self._device = resolve_input(device if device is not None else config.INPUT_DEVICE)
        self._stream: sd.InputStream | None = None

    def _callback(self, indata, frames, time_info, status):
        mono = indata[:, 0].copy()
        self.level = float(np.sqrt(np.mean((mono.astype(np.float64) / 32768.0) ** 2)) * 4.0)
        try:
            self.frames.put_nowait(mono)
        except queue.Full:
            pass                              # dropping a frame beats stalling the audio thread

    def start(self):
        self._stream = sd.InputStream(
            samplerate=config.SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=self._blocksize,
            device=self._device,
            callback=self._callback,
        )
        self._stream.start()

    @property
    def device_name(self) -> str:
        try:
            return sd.query_devices(self._device if self._device is not None else sd.default.device[0])["name"]
        except Exception:
            return str(self._device)

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def drain(self):
        """Throw away anything buffered, so a new turn starts clean."""
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                return


class Player:
    """A speaker that can be fed while it is still talking.

    Responses are spoken sentence by sentence as the model streams them, so
    playback has to accept new audio mid-sentence without clicking.
    """

    def __init__(self, samplerate: int = config.HUD_RATE, channels: int = 2):
        self.samplerate = samplerate
        self.level = 0.0
        self._chunks: collections.deque[np.ndarray] = collections.deque()
        self._lock = threading.Lock()
        self._needs_fade = True
        self._stream: sd.OutputStream | None = None
        self._channels = channels

    def _callback(self, outdata, frames, time_info, status):
        out = np.zeros(frames, dtype=np.float32)
        with self._lock:
            filled = 0
            while filled < frames and self._chunks:
                chunk = self._chunks[0]
                take = min(len(chunk), frames - filled)
                out[filled:filled + take] = chunk[:take]
                if take == len(chunk):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = chunk[take:]
                filled += take

        if filled:
            if self._needs_fade:              # 3 ms ramp, stops the click at a join
                ramp = min(filled, 144)
                out[:ramp] *= np.linspace(0.0, 1.0, ramp, dtype=np.float32)
                self._needs_fade = False
            self.level = float(np.sqrt(np.mean(out ** 2)) * 4.0)
        else:
            self._needs_fade = True           # next audio will need a ramp again
            self.level = 0.0

        if self._channels == 1:
            outdata[:, 0] = out
        else:
            outdata[:] = out[:, None]

    def start(self):
        self._stream = sd.OutputStream(
            samplerate=self.samplerate,
            channels=self._channels,
            dtype="float32",
            blocksize=1024,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def write(self, samples: np.ndarray):
        """Queue mono float32 audio for playback."""
        with self._lock:
            self._chunks.append(np.asarray(samples, dtype=np.float32))

    def playing(self) -> bool:
        with self._lock:
            return bool(self._chunks)

    def flush(self):
        with self._lock:
            self._chunks.clear()

    def wait_drained(self, timeout: float = 60.0) -> bool:
        """Block until everything queued has actually left the speaker."""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.playing():
                return True
            time.sleep(0.02)
        return False
