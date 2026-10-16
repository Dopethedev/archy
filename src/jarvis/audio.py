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
        self.gain = 1.0                      # what the software AGC is currently applying
        self._blocksize = blocksize
        self._device = resolve_input(device if device is not None else config.INPUT_DEVICE)
        self._env = 0.0                      # slow running RMS, drives the AGC
        self._noise = 0.0                    # noise floor estimate, drives the gate
        self._stream: sd.InputStream | None = None

    def _apply_gain(self, mono: np.ndarray) -> np.ndarray:
        """Boost the microphone in software.

        The headset's own AGC loops the mic into the headphones, so it is left
        off and this replaces it. Attack is slow and release is slower, which
        keeps the level steady without pumping. Silence is never amplified, or
        the noise floor would start looking like speech to the VAD.
        Silent rule: never hand out a view into PortAudio's buffer. That buffer
        is reused by the audio thread on the very next callback, so a queued
        view would silently turn into a copy of whatever was played last. Every
        path here therefore returns an independent array.
        """
        mono = np.array(mono, dtype=np.int16, copy=True)
        if not config.SOFT_AGC:
            if config.MIC_GAIN == 1.0:
                return mono
            return np.clip(mono.astype(np.float32) * config.MIC_GAIN, -32768, 32767).astype(np.int16)

        x = mono.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x)))
        # envelope rises instantly and falls slowly, so a sudden loud sound is
        # seen immediately instead of being amplified at the old quiet gain
        self._env = max(0.97 * self._env + 0.03 * rms, rms)

        # Noise floor: drops towards quiet audio quickly and climbs only very
        # slowly, so a long speech does not drag it upwards. The first frame
        # sets it outright, otherwise the opening seconds of room noise would
        # be amplified before the estimate caught up.
        if self._noise <= 0.0:
            self._noise = rms
        elif rms < self._noise:
            self._noise += (rms - self._noise) * 0.5
        else:
            self._noise += (rms - self._noise) * 0.0004

        # The gate is the whole point. Without it the room noise itself gets
        # amplified to speech level, the VAD believes the room is talking, and
        # every turn either fires on nothing or never ends.
        gate = max(config.AGC_FLOOR, self._noise * config.AGC_GATE_FACTOR)
        if self._env < gate:
            self.gain = max(1.0, self.gain * 0.9)     # below the gate: do not boost
            return mono

        target = max(1.0, min(config.AGC_TARGET / self._env, config.AGC_MAX_GAIN))
        if target < self.gain:
            self.gain = target                # cut instantly, so loud audio never clips
        else:
            self.gain += (target - self.gain) * 0.08   # ease up gently when quiet
        boosted = np.clip(x * self.gain * 32768.0, -32768, 32767)
        return boosted.astype(np.int16)

    def _callback(self, indata, frames, time_info, status):
        mono = self._apply_gain(indata[:, 0])
        self.level = float(np.sqrt(np.mean((mono.astype(np.float64) / 32768.0) ** 2)) * 4.0)
        try:
            self.frames.put_nowait(mono)
        except queue.Full:
            pass                              # dropping a frame beats stalling the audio thread

    def start(self):
        if not config.HARDWARE_AGC:
            self._disable_hardware_agc()
        self._stream = sd.InputStream(
            samplerate=config.SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=self._blocksize,
            device=self._device,
            callback=self._callback,
        )
        self._stream.start()

    @staticmethod
    def _disable_hardware_agc():
        """Keep the card's own AGC off: it is what loops the mic into the headphones."""
        import subprocess
        for card in range(4):
            for control in ("Auto Gain Control",):
                subprocess.run(["amixer", "-c", str(card), "cset", f"name={control}", "off"],
                               capture_output=True, timeout=5)

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
