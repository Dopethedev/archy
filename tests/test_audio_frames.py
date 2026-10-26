"""MicCapture must hand out arrays that own their own memory.

PortAudio reuses the buffer it passes to the callback. Returning a view of it
means every queued frame silently mutates into whatever the next callback
contained, so a 20 second recording came back as a constant tone and every
model scored it as nothing.

Compared by ownership, not by value: the software AGC deliberately drives
different inputs to the same output level, so equal values prove nothing.
"""
from pathlib import Path
import queue
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np
from archy import config
from archy.audio import MicCapture


def make_mic():
    mic = MicCapture.__new__(MicCapture)
    mic.frames = queue.Queue()
    mic.level = 0.0
    mic.gain = 1.0
    mic._env = 0.0
    mic._noise = 0.0          # the AGC's noise floor, part of its state
    return mic


def owns_its_data(a: np.ndarray) -> bool:
    """True when a is a real copy rather than a view onto someone else's buffer."""
    return a.base is None


failures = 0

for label, soft, gain in (("AGC on", True, 1.0),
                          ("gain off", False, 1.0),
                          ("fixed gain 8x", False, 8.0),
                          ("AGC on, silence", True, 1.0)):
    config.SOFT_AGC = soft
    config.MIC_GAIN = gain
    mic = make_mic()

    buf = np.zeros((480, 1), dtype=np.int16)      # the buffer PortAudio reuses
    buf[:, 0] = 0 if "silence" in label else 5000
    mic._callback(buf, 480, None, None)
    first = mic.frames.get_nowait()

    buf[:, 0] = 12000                             # the thread overwrites it
    mic._callback(buf, 480, None, None)
    second = mic.frames.get_nowait()

    ok = (first is not second
          and owns_its_data(first)
          and owns_its_data(second)
          and not np.shares_memory(first, second))
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:16s} owns data: {owns_its_data(first)}, "
          f"shares memory: {np.shares_memory(first, second)}")

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
