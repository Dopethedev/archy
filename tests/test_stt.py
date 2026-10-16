"""Speech to text, on the path the app actually uses: 16 kHz float32 in, text out.

The test audio is made with the same voice the assistant speaks with, so this
measures whisper against realistic speech rather than a robotic synthesizer.
"""
import re, time, sys
import numpy as np
from jarvis.audio import resample
from faster_whisper import WhisperModel
from kokoro_onnx import Kokoro

PHRASE = "Jarvis, what is the cpu usage and open sober for me"
WANT = {"jarvis", "cpu", "usage", "sober"}

kokoro = Kokoro("models/kokoro-v1.0.onnx", "models/voices-v1.0.bin")
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
print("PASS: production path works")
