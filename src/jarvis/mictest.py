"""Live microphone check.

Reports three things at once, using the same components as the real loop:
the level the assistant sees, whether the wake word fires, and what whisper
transcribes. If transcription is right but the wake word never fires, the
audio path is fine and the wake phrase is the problem. If the transcription
is nonsense, the audio itself is wrong.

On Linux it also reports clipping and the actual stream format pipewire is
delivering, because a silent sample-rate mismatch looks exactly like a wake
model that refuses to match. Neither is a thing on macOS or Windows, so there
it says so instead, and points at the permission those two platforms gate the
microphone behind.
"""

from __future__ import annotations

import asyncio
import queue
import tempfile
import time
from pathlib import Path

import numpy as np

from . import config
from .audio import MicCapture
from .stt import Transcriber
from .vad import SpeechVAD
from .wake import WakeWord


def _stream_format():
    """What pipewire thinks we asked for, and what it is actually running."""
    import shutil
    import subprocess
    try:
        if shutil.which("pactl") is None:          # pulse and pipewire are Linux
            return []
        out = subprocess.run(["pactl", "list", "source-outputs"], capture_output=True,
                             text=True, timeout=5).stdout
        keep = []
        for block in out.split("Source Output #"):
            if "python" in block:
                for line in block.splitlines():
                    if "Sample Specification" in line or "Source:" in line:
                        keep.append(line.strip())
        return keep
    except Exception:
        return []


def _permission_note() -> str | None:
    """Where the microphone switch is, from the one place that says."""
    from . import audio
    return audio.mic_permission_note()


async def run(seconds: float | None = None, device: str | None = None, transcribe: bool = True):
    if device:
        config.INPUT_DEVICE = device
    mic = MicCapture()
    # the detector is only built when WAKE_MODE asks for it: in speech mode nothing
    # uses it, and building it anyway meant this check could fail on a machine whose
    # assistant works fine. A model file that is missing says so instead of raising
    # out of the check that exists to diagnose exactly this.
    try:
        wake = WakeWord() if config.WAKE_MODE == "model" else None
    except Exception as exc:
        print(f"wake detector unavailable: {exc}")
        wake = None
    stt = Transcriber() if transcribe else None
    vad = SpeechVAD()
    mic.start()

    try:
        import sounddevice as sd
        name = device or sd.query_devices(sd.default.device[0])["name"]
    except Exception as exc:
        # A check that exists to diagnose a silent microphone must not stop on the
        # one thing that is hard to read on a machine whose audio is broken. The
        # levels below are still worth watching with no name to put on them.
        name = device or f"the default input ({type(exc).__name__}: {exc})"
    print(f"input device: {name}")
    for line in _stream_format():
        print(f"  pipewire: {line}")
    if note := _permission_note():
        print(f"  note: {note}")
    print("speak normally. ctrl+c to stop.\n")

    started = time.monotonic()
    last_print = 0.0
    window_level = 0.0
    peak_level = 0.0
    peak_score = 0.0
    peak_vad = 0.0
    detections = 0
    captured: list[np.ndarray] = []
    clipped = 0
    total_samples = 0
    utterance: list[np.ndarray] = []
    speaking = False
    silence_frames = 0
    frame_ms = 1000.0 * config.INPUT_BLOCK / config.SAMPLE_RATE

    try:
        while seconds is None or time.monotonic() - started < seconds:
            try:
                frame = mic.frames.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue

            level = float(np.sqrt(np.mean((frame.astype(np.float64) / 32768.0) ** 2)))
            window_level = max(window_level * 0.7, level)
            peak_level = max(peak_level, level)
            total_samples += frame.size
            clipped += int(np.sum(np.abs(frame.astype(np.int32)) >= 32700))

            if wake is not None and wake.push(frame):
                detections += 1
                print("   *** WAKE WORD DETECTED ***")

            score = float(getattr(wake, "last_score", 0.0))
            peak_score = max(peak_score, score)

            prob = float(vad.speech(frame.astype(np.int16), frame_size=frame.size))
            peak_vad = max(peak_vad, prob)
            captured.append(frame)
            if len(captured) > 16000 * 60 // max(1, frame.size):
                captured.pop(0)                       # keep the last minute
            if prob >= config.SPEECH_ON:
                speaking = True
                silence_frames = 0
                utterance.append(frame)
                # a runaway here would grow without bound on a stuck mic, the
                # same shape of bug that once cost three cores in the main loop
                if len(utterance) * frame_ms > 1000 * config.MAX_UTTERANCE_S:
                    utterance.pop(0)
            elif speaking:
                utterance.append(frame)
                silence_frames += 1
                if silence_frames * frame_ms >= config.END_SILENCE_MS:
                    audio = np.concatenate(utterance).astype(np.float32) / 32768.0
                    utterance = []
                    speaking = False
                    if stt and audio.size > 0.25 * config.SAMPLE_RATE:
                        heard = stt.transcribe(audio)
                        if heard:
                            print(f"   heard: {heard!r}", flush=True)

            now = time.monotonic()
            if now - last_print >= 0.4:
                last_print = now
                bar = "#" * min(56, int(window_level * 120))
                print(f"  {window_level:6.4f} |{bar:<56}|  speech {prob:.2f}  "
                      f"wake {peak_score:.2f}  hits {detections}", flush=True)
                window_level = 0.0
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        mic.stop()

    clip_pct = 100.0 * clipped / max(1, total_samples)
    print(f"\npeak level {peak_level:.4f}   peak speech score {peak_vad:.2f}   clipping {clip_pct:.2f}%")
    if wake is None:
        print("wake word: heard by the recogniser, not by a trained detector "
              "(WAKE_MODE is speech), so the score above is not in use")
    else:
        print(f"peak wake score {peak_score:.3f}   wake hits: {detections}")
    if captured:
        # the system's temp directory, not /tmp: this is the one line that assumed
        # a Unix path, and on Windows it wrote to a directory that does not exist
        saved_to = Path(tempfile.gettempdir()) / "mic_last.npy"
        try:
            np.save(saved_to, np.concatenate(captured))
            print(f"saved the last minute to {saved_to} for offline analysis")
        except Exception as exc:
            print(f"could not save audio: {exc}")
    if peak_level < 0.005 and detections == 0:
        print("\nThe microphone barely heard anything. If you were talking, the problem")
        print("is the mic or its gain, not the software.")
        if note := _permission_note():
            print(f"If nothing was ever allowed to listen, check: {note}.")
    if wake is not None:
        print(f"\nwake word {'fired' if detections else 'did not fire'} "
              f"(best score {peak_score:.3f}, threshold {config.WAKE_THRESHOLD})")
    # the card's own gain is an ALSA control, so the advice is Linux's alone
    if clip_pct > 1.0 and _permission_note() is None:
        print("\nWARNING: the signal is clipping. Turn Auto Gain Control back off:")
        print("  amixer -c 0 sset 'Auto Gain Control' off")
    elif clip_pct > 1.0:
        print("\nWARNING: the signal is clipping. Lower the input level in the "
              "system sound settings.")
