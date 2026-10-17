"""The software AGC must boost speech and leave the room noise alone.

Without the gate, a quiet microphone gets its own room noise amplified up to
speech level, the VAD then hears the room as talking, and turns either fire on
nothing or never end. These cases pin that behaviour down.
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np
from jarvis import config
from jarvis.audio import MicCapture

config.SOFT_AGC = True
rng = np.random.default_rng(7)

def make_mic():
    mic = MicCapture.__new__(MicCapture)
    mic._env = 0.0
    mic._noise = 0.0
    mic.gain = 1.0
    mic._focus = False          # idle posture: conservative
    return mic

def run(mic, rms, frames, freq=220.0):
    """A tone at a given level, which stands in for speech."""
    t = np.arange(config.INPUT_BLOCK * frames) / config.SAMPLE_RATE
    sig = (np.sin(2 * np.pi * freq * t) + 0.15 * rng.standard_normal(t.size)).astype(np.float32)
    sig *= (rms / np.sqrt(np.mean(sig ** 2)))
    outs = []
    for i in range(frames):
        chunk = np.clip(sig[i*config.INPUT_BLOCK:(i+1)*config.INPUT_BLOCK] * 32768, -32768, 32767)
        y = mic._apply_gain(chunk.astype(np.int16))
        outs.append(float(np.sqrt(np.mean((y.astype(np.float64)/32768.0) ** 2))))
    return outs

failures = 0
def check(label, ok, detail):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:36s} {detail}")

mic = make_mic()
run(mic, 0.0008, 400)                       # settle on a room-noise floor
ambient = run(mic, 0.0008, 100)[-1]
check("room noise stays at room level", ambient < 0.005, f"out {ambient:.5f}")

speech = run(mic, 0.0100, 200)[-1]
check("quiet speech is boosted", speech > 0.06, f"out {speech:.5f} (target {config.AGC_TARGET})")

loud = run(mic, 0.3000, 30)
check("loud speech never clips", max(loud) < 0.9, f"max out {max(loud):.3f}")

after = run(mic, 0.0008, 200)[-1]
check("noise is left alone again after speech", after < 0.005, f"out {after:.5f}")

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
