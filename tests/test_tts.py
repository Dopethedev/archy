"""TTS bake-off: piper vs kokoro.

Synthesize the same sentence with each, time it, then feed the result back
through whisper. If whisper can read it back, the voice is genuinely
intelligible rather than just plausible-sounding.
"""
import time, wave, subprocess, sys
import numpy as np
from jarvis.audio import resample

TEXT = "Systems online. Your cpu usage is twelve percent and I have opened it for you."

def save(path, int16, sr):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(int16.tobytes())

def load16k(path):
    with wave.open(path) as w:
        sr = w.getframerate()
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    return resample(raw, sr, 16000)

results = {}

# ---------- piper ----------
t = time.monotonic()
p = subprocess.run(
    ["uv", "run", "piper", "-m", "models/en_GB-alan-medium.onnx", "-f", "/tmp/out_piper.wav"],
    input=TEXT, text=True, capture_output=True, cwd="/home/dope/jarvis")
if p.returncode != 0:
    print("piper FAILED:", p.stderr[-400:])
else:
    dt = time.monotonic() - t
    with wave.open("/tmp/out_piper.wav") as w:
        dur = w.getnframes() / w.getframerate()
    results["piper"] = (dt, dur, "/tmp/out_piper.wav")
    print(f"piper : {dt:.2f}s for {dur:.2f}s audio  ({dur/dt:.1f}x realtime)")

# ---------- kokoro ----------
try:
    from kokoro_onnx import Kokoro
    t = time.monotonic()
    k = Kokoro("models/kokoro-v1.0.onnx", "models/voices-v1.0.bin")
    load = time.monotonic() - t
    voice = "bm_george"          # British male
    try:
        samples, sr = k.create(TEXT, voice=voice, speed=1.0, lang="en-gb")
    except TypeError:            # older/newer signature
        samples, sr = k.create(TEXT, voice=voice, speed=1.0)
    dt = time.monotonic() - t
    audio = np.asarray(samples, dtype=np.float32)
    save("/tmp/out_kokoro.wav", (audio * 32767).astype(np.int16), sr)
    dur = len(audio) / sr
    results["kokoro"] = (dt, dur, "/tmp/out_kokoro.wav")
    print(f"kokoro: load {load:.2f}s, then {dt:.2f}s for {dur:.2f}s audio ({dur/dt:.1f}x realtime)")
except Exception as e:
    print(f"kokoro FAILED: {type(e).__name__}: {e}")

# ---------- intelligibility round trip ----------
print("\n--- whisper reading each back ---")
from faster_whisper import WhisperModel
m = WhisperModel("base.en", device="cpu", compute_type="int8", cpu_threads=6)
want = {"systems", "online", "cpu", "usage", "twelve", "percent", "opened"}
allok = True
for name, (dt, dur, path) in results.items():
    audio = load16k(path)
    peak = np.abs(audio).max()
    segs, _ = m.transcribe((audio / max(1.0, peak)).astype(np.float32), beam_size=1, language="en")
    text = " ".join(s.text.strip() for s in segs).strip()
    # the voices say numbers as digits ("12%"), so normalise before comparing
    norm = text.lower().replace(",", " ").replace(".", " ").replace("%", " percent ")
    norm = norm.replace("12", "twelve")
    got = set(norm.split())
    miss = want - got
    ok = not miss
    allok &= ok
    print(f"  {name:7s} {'OK  ' if ok else 'MISS'} {text!r}")
    if miss:
        print(f"          missing: {miss}")

sys.exit(0 if allok else 1)
