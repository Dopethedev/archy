"""The microphone check, run against a stub microphone.

This is the one thing a person runs when the assistant cannot hear them, so it has
to work on a machine whose audio is already broken: no device to open, a name that
cannot be read, a wake model that will not load. It also has to be right about what
it reports, because the whole point is telling a broken microphone apart from a
broken recogniser.

It did none of that. It called `vad.predict`, which has not existed on that class
since the voice activity model was rewritten to run on onnxruntime directly, so the
check died with an AttributeError on the first frame and the person looking at it
learned nothing at all. Nothing tested this loop, which is why it stayed broken.

The stub microphone means none of the checks below need a real one.
"""
import asyncio
import contextlib
import io
import queue
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from archy import config  # noqa: E402
from archy import mictest  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:56s} {detail}")


FRAME = config.INPUT_BLOCK


def silence(level=20):
    return np.full(FRAME, level, dtype=np.int16)


def speech(level=9000):
    """A frame the stub detector calls speech, by how loud it is."""
    wave = (np.sin(np.linspace(0, 40, FRAME)) * level).astype(np.int16)
    return wave


def clipping():
    return np.full(FRAME, 32767, dtype=np.int16)


def build(frames, transcribed="this is a request"):
    """A microphone, detector and recogniser that behave, and a way to read them."""
    state = {"heard": [], "stopped": False}

    class FakeMic:
        def __init__(self, *a, **k):
            self.frames = queue.Queue(maxsize=1000)
            for frame in frames:
                self.frames.put(frame)
        def start(self):
            pass
        def stop(self):
            state["stopped"] = True

    class FakeWake:
        def __init__(self, *a, **k):
            raise RuntimeError("no wake model here")

    class FakeVAD:
        def __init__(self, *a, **k):
            pass
        def speech(self, frame, frame_size=None):
            return 0.9 if float(np.abs(frame).mean()) > 200 else 0.01

    class FakeSTT:
        def __init__(self, *a, **k):
            pass
        def transcribe(self, audio):
            state["heard"].append(audio)
            return transcribed

    mictest.MicCapture = FakeMic
    mictest.WakeWord = FakeWake
    mictest.Transcriber = FakeSTT
    mictest.SpeechVAD = FakeVAD
    return state


def run(frames, seconds=1.0, transcribed="this is a request"):
    state = build(frames, transcribed)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        asyncio.run(mictest.run(seconds=seconds))
    return state, out.getvalue()


print("-- it finishes at all --")
# 30 ms frames, so this is about a second and a half of audio: a little room noise,
# then a spoken request, then enough quiet for the end-of-speech rule to fire
frames = [silence()] * 5 + [speech()] * 20 + [silence()] * 26
state, said = run(frames, seconds=2.0)
check("the check runs to the end instead of dying on the first frame",
      "peak level" in said, said.strip().splitlines()[-1][:60])
check("and it closed the microphone behind itself", state["stopped"] is True)

print("-- and it hears what was said --")
check("the utterance reaches the recogniser", len(state["heard"]) == 1,
      str(len(state["heard"])))
if state["heard"]:
    heard_audio = state["heard"][0]
    seconds_sent = heard_audio.size / config.SAMPLE_RATE
    # the speech, plus the 700 ms of quiet that is what ends it, and none of the
    # room noise in front of it: the recording starts on the first spoken frame
    check("the recording is the speech plus the quiet that ended it",
          1.2 < seconds_sent < 1.5, f"{seconds_sent:.2f}s of audio")
    # the recording is handed over as float32 in -1..1, so a spoken frame's mean
    # magnitude is fractions of a unit and a room-noise frame's is thousandths
    check("and it starts at the speech, not at the noise before it",
          float(np.abs(heard_audio[:FRAME]).mean()) > 0.01,
          f"first frame mean {float(np.abs(heard_audio[:FRAME]).mean()):.3f}")
check("and the transcript is printed for the person watching",
      "this is a request" in said, [ln for ln in said.splitlines() if "heard" in ln][:1])

print("-- a missing wake model is reported, not fatal --")
real_mode = config.WAKE_MODE
try:
    config.WAKE_MODE = "model"           # the mode that loads the trained detector
    state, said = run([silence()] * 10 + [speech()] * 10 + [silence()] * 26, seconds=2.0)
finally:
    config.WAKE_MODE = real_mode
check("a detector that will not load is named instead of raised",
      "wake detector unavailable" in said and "no wake model here" in said,
      [ln for ln in said.splitlines() if "detector" in ln][:1])
check("and the check still reports the levels", "peak level" in said)

state, said = run([silence()] * 10, seconds=1.0)
check("in speech mode it says the trained detector is not the one in use",
      "WAKE_MODE is speech" in said)

print("-- nothing at all still reports something --")
state, said = run([silence(level=0)] * 40)
check("a silent microphone is called silent, with the reason it gives",
      "barely heard anything" in said, [ln for ln in said.splitlines()
                                        if "barely" in ln][:1])
check("and no transcript is invented from it", state["heard"] == [])

print("-- and a clipping one says so --")
state, said = run([speech()] * 10 + [clipping()] * 20 + [silence()] * 8)
check("clipping is reported with the number it measured",
      "clipping" in said and "%" in said,
      [ln for ln in said.splitlines() if "clipping" in ln][:1])

print("-- the levels and the counts --")
check("a peak level is printed", "peak level" in said,
      [ln for ln in said.splitlines() if ln.startswith("peak level")][:1])
check("no microphone name is invented when the device cannot be asked",
      "input device:" in said, [ln for ln in said.splitlines() if "input device" in ln][:1])

print()
print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
sys.exit(0 if not failed else 1)
