"""Wake word: does 'hey archy' fire on speech, and stay quiet on everything else?"""
import numpy as np, os, sys
from archy.audio import resample
import openwakeword
from openwakeword.model import Model
from kokoro_onnx import Kokoro

MODELS = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
HEY = os.path.join(MODELS, "hey_jarvis_v0.1.onnx")
oww = Model(wakeword_model_paths=[HEY])
LABEL = list(oww.models.keys())[0]
print(f"wake model: {LABEL}")

k = Kokoro("models/kokoro-v1.0.onnx", "models/voices-v1.0.bin")

def say(text):
    samples, sr = k.create(text, voice="bm_george", speed=1.0, lang="en-gb")
    a = resample(np.asarray(samples, dtype=np.float32), sr, 16000)
    return (a / max(1e-6, np.abs(a).max()) * 30000).astype(np.int16)

def peak_of(audio):
    oww.reset()
    frames = oww.predict_clip(audio)
    return max(f.get(LABEL, 0.0) for f in frames)

print("\n--- should FIRE ---")
pos = {
    "hey archy": peak_of(say("Hey Archy")),
    "hey archy + request": peak_of(say("Hey Archy, what is the weather")),
    "hey archy (slower)": peak_of(say("Hey, Archy.")),
}
for t, v in pos.items():
    print(f"  {t:24s} {v:.3f}")

print("\n--- should stay QUIET ---")
neg = {
    "hello there": peak_of(say("Hello there, how are you doing today")),
    "hey marvin": peak_of(say("Hey Marvin, are you around")),
    "travis": peak_of(say("Travis is a good name")),
    "silence": peak_of(np.zeros(16000 * 2, dtype=np.int16)),
    "noise": peak_of((np.random.default_rng(0).standard_normal(16000 * 2) * 1500).astype(np.int16)),
}
for t, v in neg.items():
    print(f"  {t:24s} {v:.3f}")

bp, bn = max(pos.values()), max(neg.values())
print(f"\nbest positive {bp:.3f} vs best negative {bn:.3f}")
if bp > 0.5 and bp > bn * 2:
    print("PASS: wake word clears the noise cleanly")
else:
    print("NOTE: margin is thin on synthetic speech; a live-mic check is needed")
