"""Microphone capture and speaker playback.

On Linux both devices are opened through pipewire, which resamples for us: the
headset hardware only runs at 48 kHz, while whisper, silero and openWakeWord all
want 16 kHz. Asking PortAudio for the raw hw device fails; the default pipewire
device accepts anything. On macOS and Windows the same stream opens through
CoreAudio and WASAPI, which do the same resampling, so nothing above this file
has to know which one it is sitting on.
"""

from __future__ import annotations

import collections
import queue
import sys
import threading
import time

import numpy as np
import sounddevice as sd
import soxr

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


def resample(audio: np.ndarray, samplerate: int, target: int) -> np.ndarray:
    """Move audio between sample rates.

    Used where a speech engine hands back its own rate (kokoro 24 kHz, piper
    22.05 kHz) and by the tests, which generate speech at one rate and feed it
    to a model that wants another.

    soxr rather than scipy: scipy was 107 MB of memory, 660 ms of start-up and
    123 MB of disk for this one call. Verified against it, same length,
    correlation 0.999, same level, and about twice as fast.
    """
    audio = np.ascontiguousarray(audio, dtype=np.float32)
    if samplerate == target:
        return audio
    return np.asarray(soxr.resample(audio, samplerate, target, quality="HQ"),
                      dtype=np.float32)


def mic_permission_note() -> str | None:
    """Where the microphone switch is, on a platform that has one.

    One home for it: the live microphone check, the failure to open, and the
    twenty seconds of silence all point at the same setting on a Mac, and three
    copies of the same sentence is how they drift apart.
    """
    if sys.platform == "darwin":
        return ("macOS gates the microphone: System Settings, Privacy and Security, "
                "Microphone, and the terminal needs a tick there before it is started")
    if sys.platform == "win32":
        return ("Windows gates the microphone: Settings, Privacy and security, "
                "Microphone, and 'let desktop apps access your microphone'")
    return None


def silence_warning() -> str:
    """What is said after twenty seconds of exact digital silence.

    A Mac that was never given the Microphone permission looks exactly like a muted
    microphone from here, and the permission is the common one by a distance, so it
    is named rather than left as "it may be switched off".
    """
    note = mic_permission_note()
    if note:
        return f"I am not hearing anything from the microphone. {note}."
    return ("I am not hearing anything from the microphone. It may be switched off, "
            "or the wrong input may be selected.")


def open_error(exc: Exception) -> str:
    """A microphone that will not open, in words worth reading.

    macOS is the one platform here that gates the microphone. A terminal that has
    never been allowed to listen gets a PortAudio error naming nothing useful,
    and the permission prompt only appears the first time a program actually
    asks, so the failure arrives before the dialog does if the dialog was
    dismissed once. Naming the switch is the difference between a two-minute fix
    and an evening of blaming the microphone.

    The original text is kept: this adds a likely cause, it does not replace
    evidence.
    """
    if sys.platform == "darwin":
        return (f"the microphone would not open ({exc}). On macOS this is usually "
                f"the Microphone permission: System Settings, Privacy and "
                f"Security, Microphone, and the app running archy needs a tick "
                f"there. A terminal that has never been allowed to listen has to "
                f"be started again after the tick.")
    if sys.platform == "win32":
        return (f"the microphone would not open ({exc}). On Windows this is "
                f"usually the Microphone privacy setting, or another program "
                f"holding the device in exclusive mode.")
    return (f"the microphone would not open ({exc}). On Linux this is usually "
            f"another program holding the device, or the wrong input device "
            f"selected: try --input followed by part of its name.")


class DeafMic:
    """Stands in when there is no microphone to be had.

    A device name that matches nothing raised out of MicCapture's constructor,
    which is called from Assistant's, which is called before the try that exists
    to survive a part failing. One stale INPUT_DEVICE in config.local.json, or one
    mistyped --input, ended the run with a traceback: no dashboard, no typed
    input, nothing, from a setting about the microphone. This has the shape the
    rest of the code asks for, hears nothing, and carries the reason, which is
    how the recogniser and the voice already degrade.
    """

    level = 0.0
    gain = 1.0

    def __init__(self, error: Exception):
        self.error = error
        self.frames: queue.Queue[np.ndarray] = queue.Queue()
        print(f"[mic] no microphone, so it cannot hear you: {error}", flush=True)

    def set_focus(self, on: bool):
        pass

    @property
    def device_name(self) -> str:
        return "none"

    def start(self):
        # reported rather than raised at the caller, so the startup path says why
        # and carries on with everything that does not need a microphone
        raise RuntimeError(str(self.error))

    def stop(self):
        pass

    def silent_seconds(self) -> float:
        # zero, so the twenty second notice does not also claim a switch that is
        # off: the reason here is the name, and it has already been said
        return 0.0

    def drain(self):
        while True:
            try:
                self.frames.get_nowait()
            except queue.Empty:
                return


class MicCapture:
    """16 kHz mono int16 frames, handed over a plain thread-safe queue.

    The audio state below is declared at class level as well as set in
    __init__. Tests build this object with __new__ to exercise the gain stage on
    its own, and three of them broke in turn as each new field was added. A
    default that already exists cannot be forgotten.
    """

    _env = 0.0                           # slow running RMS, drives the AGC
    _noise = 0.0                         # noise floor estimate, drives the gate
    _focus = False                       # true while waiting for a request
    gain = 1.0                           # what the software AGC is applying
    level = 0.0                          # 0..1, for the HUD waveform

    def __init__(self, blocksize: int = config.INPUT_BLOCK, device=None):
        self.frames: queue.Queue[np.ndarray] = queue.Queue(maxsize=200)
        self.level = 0.0                     # 0..1, for the HUD waveform
        self.gain = 1.0                      # what the software AGC is currently applying
        self._blocksize = blocksize
        self._device = resolve_input(device if device is not None else config.INPUT_DEVICE)
        self._env = 0.0                      # slow running RMS, drives the AGC
        self._noise = 0.0                    # noise floor estimate, drives the gate
        self._focus = False                  # true while waiting for a request
        # when signal was last seen, so a microphone delivering nothing at all
        # can be told apart from a room that is merely quiet. Any live input has
        # a noise floor and its samples are not all zero, so exact zeros are the
        # one thing a working microphone never produces.
        self._signal_at = time.monotonic()
        self._stream: sd.InputStream | None = None

    def set_focus(self, on: bool):
        """Listen harder while a request is expected.

        The same microphone settings cannot suit both jobs. Idle only has to
        notice a wake phrase, where being conservative is free because a missed
        word just means saying it again. Waiting for a request is the one moment
        where a quiet word is lost for good.
        """
        self._focus = bool(on)

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
        # every turn either fires on nothing or never ends. While waiting for a
        # request the gate is looser, so a soft word still gets through.
        gate_factor = (config.AGC_GATE_FACTOR_FOCUSED if self._focus
                       else config.AGC_GATE_FACTOR)
        gate = max(config.AGC_FLOOR, self._noise * gate_factor)
        if self._env < gate:
            self.gain = max(1.0, self.gain * 0.9)     # below the gate: do not boost
            return mono

        target_rms = config.AGC_TARGET_FOCUSED if self._focus else config.AGC_TARGET
        target = max(1.0, min(target_rms / self._env, config.AGC_MAX_GAIN))
        if target < self.gain:
            self.gain = target                # cut instantly, so loud audio never clips
        else:
            self.gain += (target - self.gain) * 0.08   # ease up gently when quiet
        boosted = np.clip(x * self.gain * 32768.0, -32768, 32767)
        return boosted.astype(np.int16)

    def _callback(self, indata, frames, time_info, status):
        mono = self._apply_gain(indata[:, 0])
        self.level = float(np.sqrt(np.mean((mono.astype(np.float64) / 32768.0) ** 2)) * 4.0)
        # A frame of exact zeros, every sample, is not a quiet room: it is a
        # microphone that is switched off, one whose boom is up, or a USB input
        # that has stalled. Keeping the time signal was last seen is what lets
        # the assistant say it cannot hear anything instead of waiting to be
        # spoken to forever. getattr because tests drive this callback on an
        # object built with __new__.
        if mono.any():
            self._signal_at = time.monotonic()
        try:
            self.frames.put_nowait(mono)
        except queue.Full:
            pass                              # dropping a frame beats stalling the audio thread

    def start(self):
        if self._stream is not None:              # never leak a second stream
            self.stop()
        if not config.HARDWARE_AGC:
            self._disable_hardware_agc()
        try:
            self._stream = sd.InputStream(
                samplerate=config.SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=self._blocksize,
                device=self._device,
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            # The constructor has already opened the device by the time it returns,
            # so a stream that then fails to start has to be closed rather than
            # dropped: PortAudio holds the device until it is, and every later
            # attempt to open it fails with the device busy, which reads like a
            # microphone that has stopped working rather than a stray handle.
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    # a close that fails is not worth replacing the reason the
                    # open did
                    pass
            self._stream = None
            raise RuntimeError(open_error(exc)) from exc

    @staticmethod
    def _disable_hardware_agc():
        """Keep the card's own AGC off: it is what loops the mic into the headphones.

        A nicety, so it fails quietly. `amixer` belongs to alsa-utils, which a
        PipeWire-only box need not have installed, and the call used to be
        unguarded: a missing binary raised out of the microphone open and the
        assistant never started listening. Nothing about turning the card's AGC
        off is worth the microphone.
        """
        import shutil
        import subprocess
        if shutil.which("amixer") is None:
            return
        for card in range(4):
            for control in ("Auto Gain Control",):
                try:
                    subprocess.run(["amixer", "-c", str(card), "cset", f"name={control}", "off"],
                                   capture_output=True, timeout=5)
                except (OSError, subprocess.SubprocessError):
                    return

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

    def silent_seconds(self) -> float:
        """How long the microphone has delivered nothing at all, in seconds."""
        return time.monotonic() - getattr(self, "_signal_at", time.monotonic())

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

    _speaking = False            # a block just handed to the device, for __new__ builds

    def __init__(self, samplerate: int = config.HUD_RATE, channels: int = 2):
        self.samplerate = samplerate
        self.level = 0.0
        self._chunks: collections.deque[np.ndarray] = collections.deque()
        self._lock = threading.Lock()
        self._needs_fade = True
        self._stream: sd.OutputStream | None = None
        self._channels = channels
        self._generation = 0

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
        # A block out of the queue is not a block that has been heard: this one is
        # in the device's own buffer and plays after this callback returns. Every
        # reader of "the reply is over" was reading an empty queue as finished, so
        # the microphone went back to listening over the last word of the answer.
        self._speaking = bool(filled)

        if self._channels == 1:
            outdata[:, 0] = out
        else:
            outdata[:] = out[:, None]

    def start(self):
        if self._stream is not None:              # same guard as the microphone
            self.stop()
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
        # no callbacks are coming any more, so the last one's answer would stand
        # forever and every later wait_drained would sit out its whole timeout
        self._speaking = False

    def write(self, samples: np.ndarray, generation: int | None = None) -> bool:
        """Queue mono float32 audio for playback. False if the reply is over.

        A reply that gets interrupted is usually mid-synthesis at that moment,
        and a sentence takes seconds to synthesise. Without the generation check
        that sentence lands here just after the flush and is heard as a fragment
        of the very answer the user cut off, mixed into the new one.
        """
        with self._lock:
            if generation is not None and generation != self._generation:
                return False
            self._chunks.append(np.asarray(samples, dtype=np.float32))
            return True

    def start_reply(self) -> int:
        """Begin a reply, invalidating any audio left over from a previous one."""
        with self._lock:
            self._generation += 1
            return self._generation

    def playing(self) -> bool:
        """Whether audio is still on its way out: queued, or already at the device."""
        with self._lock:
            return bool(self._chunks) or self._speaking

    def flush(self):
        """Drop what is queued, and invalidate audio still being synthesised."""
        with self._lock:
            self._chunks.clear()
            self._generation += 1
            self._speaking = False

    def wait_drained(self, timeout: float = 60.0) -> bool:
        """Block until everything queued has actually left the speaker.

        Includes the block already handed to the device, so this returns within one
        buffer of the last sound rather than up to a block before it.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.playing():
                return True
            time.sleep(0.02)
        return False
