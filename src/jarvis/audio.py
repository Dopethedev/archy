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


class MicCapture:
    """16 kHz mono int16 frames, handed over a plain thread-safe queue."""

    def __init__(self, blocksize: int = config.INPUT_BLOCK):
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self.level = 0.0                     # 0..1, for the HUD waveform
        self._blocksize = blocksize
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
            callback=self._callback,
        )
        self._stream.start()

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
