"""Speech to text, on the path the app actually uses: 16 kHz float32 in, text out.

The test audio is made with the same voice the assistant speaks with, so this
measures whisper against realistic speech rather than a robotic synthesizer.
"""
import os, re, tempfile, time, sys
from pathlib import Path

# The shipped defaults, not this machine's: a fresh clone has no config.local.json,
# so what wakes it there is whatever config.py brings.
_shipped = Path(tempfile.mkdtemp(prefix="archy-stt-")) / "config.local.json"
_shipped.write_text("{}")
os.environ["ARCHY_LOCAL_CONFIG"] = str(_shipped)

import voice_models                              # noqa: E402
import numpy as np                               # noqa: E402
from archy import config                         # noqa: E402
from archy.audio import resample                 # noqa: E402
from archy.wake import SpeechWake                # noqa: E402
from faster_whisper import WhisperModel          # noqa: E402
from kokoro_onnx import Kokoro                   # noqa: E402

voice_models.require("this group measures whisper against speech it speaks itself")

PHRASE = "Archy, what is the cpu usage and open sober for me"
WANT = {"cpu", "usage", "sober"}

kokoro = Kokoro(str(voice_models.MODEL), str(voice_models.VOICES))
samples, sr = kokoro.create(PHRASE, voice="bm_george", speed=1.0, lang="en-gb")
audio = resample(np.asarray(samples, dtype=np.float32), sr, 16000)
audio = (audio / max(1e-6, np.abs(audio).max())).astype(np.float32)
print(f"audio: {len(audio)/16000:.2f}s @16k")

t = time.monotonic()
model = WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=6)
print(f"model load: {time.monotonic()-t:.2f}s")

t = time.monotonic()
segs, info = model.transcribe(audio, beam_size=1, language="en")
text = " ".join(s.text.strip() for s in segs).strip()
dt = time.monotonic() - t
dur = len(audio) / 16000
print(f"transcribe: {dt:.2f}s for {dur:.2f}s audio ({dur/dt:.1f}x realtime)")
print(f"heard: {text!r}")

got = set(re.findall(r"[a-z0-9]+", text.lower()))
if miss := WANT - got:
    print(f"FAIL: missing {miss}")
    sys.exit(1)

# The name is the part the recogniser is free to spell: it wrote "Archie" for
# this one. The call has to survive that, which is what WAKE_ALIASES is for, so
# the check is that the shipped pairing wakes on what whisper actually produced
# rather than that whisper spelled the project's name the way the project does.
print(f"heard the name as {text.split(',')[0]!r}, shipped phrase {config.WAKE_PHRASE!r}")
woke, request = SpeechWake().match(text)
if not woke:
    print("FAIL: the shipped phrase does not wake on the spelling the recogniser produced")
    sys.exit(1)
if "cpu usage" not in request.lower():
    print(f"FAIL: the request after the name did not survive: {request!r}")
    sys.exit(1)
print("PASS: production path works")
