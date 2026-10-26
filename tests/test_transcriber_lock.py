"""One transcription at a time.

The model is not meant to run concurrently. Several overlapping transcriptions
on a six-thread model thrash the machine instead of finishing sooner: this was
measuring nearly three cores busy for minutes with no output to show for it.
"""
from pathlib import Path
import sys, threading, time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np
from archy import config
from archy.stt import Transcriber

failures = 0
def check(label, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:44s} {detail}")


class SlowModel:
    """Stands in for whisper, and notices if two calls ever overlap."""
    def __init__(self):
        self.live = 0
        self.peak = 0
        self.calls = 0
        self.lock = threading.Lock()

    def transcribe(self, audio, **kwargs):
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
            self.calls += 1
        time.sleep(0.15)                      # long enough to overlap if allowed
        with self.lock:
            self.live -= 1
        return [], None


t = Transcriber.__new__(Transcriber)          # no model load, no memory cost
t._lock = threading.Lock()
model = SlowModel()
t._model = model

audio = (np.random.default_rng(0).standard_normal(config.SAMPLE_RATE)
         * 0.05).astype(np.float32)

threads = [threading.Thread(target=t.transcribe, args=(audio,)) for _ in range(6)]
start = time.monotonic()
for th in threads:
    th.start()
for th in threads:
    th.join()
elapsed = time.monotonic() - start

check("all six calls ran", model.calls == 6, f"calls={model.calls}")
check("never two at once", model.peak == 1, f"peak concurrency={model.peak}")
check("serialised, not parallel", elapsed >= 6 * 0.15 * 0.9,
      f"{elapsed:.2f}s for six 0.15s jobs (parallel would be ~0.15s)")

# and the guard rails around empty audio still hold
check("digital silence is refused", t.transcribe(np.zeros(config.SAMPLE_RATE, dtype=np.float32)) == "")
check("audio too short is refused", t.transcribe(np.zeros(100, dtype=np.float32)) == "")

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
