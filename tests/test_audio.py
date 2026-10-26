"""Audio path test: mic in, speaker out, and a real acoustic loop.

Play 'Hey Archy' out of the speaker while the mic records, then run the
wake detector on what the mic actually captured. That exercises the whole
acoustic chain rather than piping buffers around in memory.
"""
import time, os, sys
import numpy as np
from archy.audio import resample
import sounddevice as sd
import openwakeword
from openwakeword.model import Model
from kokoro_onnx import Kokoro

print("default in :", sd.query_devices(sd.default.device[0])['name'])
print("default out:", sd.query_devices(sd.default.device[1])['name'])

# --- 1. plain capture ---
t = time.monotonic()
rec = sd.rec(int(1.5 * 16000), samplerate=16000, channels=1, dtype='int16')
sd.wait()
cap = rec.flatten()
print(f"\ncapture: {len(cap)} samples in {time.monotonic()-t:.2f}s, "
      f"rms={np.sqrt(np.mean(cap.astype(np.float64)**2)):.0f}, peak={np.abs(cap).max()}")

# --- 2. make the wake phrase ---
k = Kokoro("models/kokoro-v1.0.onnx", "models/voices-v1.0.bin")
samples, sr = k.create("Hey Archy", voice="bm_george", speed=1.0, lang="en-gb")
voice = np.asarray(samples, dtype=np.float32)
out = resample(voice, sr, 48000)

# --- 3. acoustic loop: play it, record it ---
frames = []
def cb(indata, n, tinfo, status):
    frames.append(indata.copy())
stream = sd.InputStream(samplerate=16000, channels=1, dtype='int16', callback=cb)
stream.start()
time.sleep(0.3)                       # a little room tone first
sd.play(out * 1.0, 48000)
sd.wait()
time.sleep(0.7)                       # and after
stream.stop(); stream.close()
heard = np.concatenate(frames).flatten()
print(f"loop capture: {len(heard)/16000:.2f}s, "
      f"rms={np.sqrt(np.mean(heard.astype(np.float64)**2)):.0f}")

# --- 4. did the mic actually hear the wake phrase? ---
MODELS = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
oww = Model(wakeword_model_paths=[os.path.join(MODELS, "hey_jarvis_v0.1.onnx")])
label = list(oww.models.keys())[0]
oww.reset()
peak = max((f.get(label, 0.0) for f in oww.predict_clip(heard)), default=0.0)
print(f"\nwake score on what the mic heard: {peak:.3f}")

if peak > 0.5:
    print("PASS: mic and speaker both work, full acoustic path verified")
elif np.abs(heard).max() > 0:
    print("INCONCLUSIVE: mic is capturing, but the headset did not leak the")
    print("              playback back into its own mic (closed-back). Needs")
    print("              a live human voice to finish verifying.")
else:
    print("FAIL: mic captured pure silence")
    sys.exit(1)
