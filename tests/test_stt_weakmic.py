"""Transcription accuracy under the conditions this microphone actually has.

The headset delivers speech at about 0.001 RMS with peaks near 440 counts out
of 32767. That is roughly nine bits of resolution, so the audio whisper gets is
coarse before anything is amplified. This test reproduces that, because it is
the real reason words come out wrong.
"""
from pathlib import Path
import sys, time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis.audio import resample  # noqa: E402
import numpy as np
from kokoro_onnx import Kokoro
from faster_whisper import WhisperModel
from jarvis import config

PHRASE = "Jarvis, open Sober and tell me my cpu usage"
WANT = {"jarvis", "open", "sober", "cpu", "usage"}

kokoro = Kokoro("models/kokoro-v1.0.onnx", "models/voices-v1.0.bin")
samples, sr = kokoro.create(PHRASE, voice="bm_george", speed=1.0, lang="en-gb")
clean = resample(np.asarray(samples, dtype=np.float32), sr, 16000)
clean = clean / np.abs(clean).max()
print(f"phrase: {PHRASE!r}\n")

# the vocabulary hint is the machine owner's business, so this test states
# the one it needs instead of inheriting it: the point is that a hint makes
# coarse audio legible, and that has to hold on a fresh checkout
config.WHISPER_HINT = ("Jarvis, Sober, cpu, usage, Hyprland, terminal, "
                       "wifi, bluetooth, screenshot")

def degrade(a, peak_counts, noise_rms):
    """Bring it down to the microphone's real level, then add its noise floor."""
    scaled = (a / np.abs(a).max()) * (peak_counts / 32768.0)
    noisy = scaled + np.random.default_rng(3).standard_normal(a.size) * noise_rms
    return np.clip(noisy * 32768, -32768, 32767).astype(np.int16)

rng = np.random.default_rng(3)
variants = {
    "clean, full scale": (clean * 32767 * 0.8).astype(np.int16),
    "weak mic, peaks ~440": degrade(clean, 440, 0.0008),
    "weak mic, peaks ~900, noisier": degrade(clean, 900, 0.0020),
}

model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8",
                     cpu_threads=config.WHISPER_THREADS)
print(f"model: {config.WHISPER_MODEL}, beam {config.WHISPER_BEAM}\n")

failures = 0
for label, audio in variants.items():
    f = audio.astype(np.float32) / 32768.0
    f = f / max(1e-6, np.abs(f).max())              # what stt.py does before transcribing
    t = time.monotonic()
    segs, _ = model.transcribe(f, beam_size=config.WHISPER_BEAM, language="en",
                               condition_on_previous_text=False,
                               initial_prompt=config.WHISPER_HINT,
                               no_speech_threshold=0.6, log_prob_threshold=-1.0,
                               temperature=[0.0, 0.2, 0.4])
    text = " ".join(s.text.strip() for s in segs).strip()
    dt = time.monotonic() - t
    got = set(text.lower().replace(",", " ").replace(".", " ").split())
    miss = WANT - got
    ok = not miss
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'MISS'}] {label:30s} {dt:5.2f}s  {text!r}")
    if miss:
        print(f"          missed: {miss}")

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' variant(s) missed'}")
sys.exit(1 if failures else 0)
