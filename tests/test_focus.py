"""While it waits for a request, it listens harder.

Idle only has to notice a wake phrase, and being conservative there costs
nothing: a missed word means saying it again. The seconds after the beep are
the one moment where a quiet word is lost for good, so the gain goes up and the
gate comes down.

The trade being checked: quieter speech gets through, but room noise must still
not be amplified into something the VAD would call speech.
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np
from archy import config
from archy.audio import MicCapture

config.SOFT_AGC = True
rng = np.random.default_rng(11)

failures = 0
def check(label, ok, detail):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:44s} {detail}")


def make_mic(focused: bool):
    mic = MicCapture.__new__(MicCapture)
    mic._env = 0.0
    mic._noise = 0.0
    mic.gain = 1.0
    mic._focus = focused
    return mic


def run(mic, rms, frames, freq=220.0):
    """A tone at a given level, standing in for speech."""
    t = np.arange(config.INPUT_BLOCK * frames) / config.SAMPLE_RATE
    sig = (np.sin(2 * np.pi * freq * t) + 0.15 * rng.standard_normal(t.size)).astype(np.float32)
    sig *= (rms / np.sqrt(np.mean(sig ** 2)))
    outs = []
    for i in range(frames):
        chunk = np.clip(sig[i * config.INPUT_BLOCK:(i + 1) * config.INPUT_BLOCK] * 32768,
                        -32768, 32767)
        y = mic._apply_gain(chunk.astype(np.int16))
        outs.append(float(np.sqrt(np.mean((y.astype(np.float64) / 32768.0) ** 2))))
    return outs


def speech_level(focused: bool) -> float:
    mic = make_mic(focused)
    run(mic, 0.0008, 300)                      # settle on the room floor
    return run(mic, 0.0100, 200)[-1]           # then a soft word


def noise_level(focused: bool) -> float:
    mic = make_mic(focused)
    run(mic, 0.0008, 300)
    return run(mic, 0.0008, 200)[-1]


idle_speech = speech_level(False)
focus_speech = speech_level(True)
check("soft speech comes through louder while waiting",
      focus_speech > idle_speech * 1.2,
      f"{focus_speech:.4f} focused against {idle_speech:.4f} idle")

idle_noise = noise_level(False)
focus_noise = noise_level(True)
check("room noise is still not turned into speech",
      focus_noise < 0.02 and focus_noise < focus_speech / 5,
      f"{focus_noise:.5f} focused against {focus_speech:.4f} speech")

check("the gain actually reaches the focused target",
      focus_speech > config.AGC_TARGET_FOCUSED * 0.7,
      f"{focus_speech:.4f} against a target of {config.AGC_TARGET_FOCUSED}")

# loud speech must not clip in either posture
for focused in (False, True):
    mic = make_mic(focused)
    loud = run(mic, 0.3000, 30)
    check(f"loud speech never clips ({'focused' if focused else 'idle'})",
          max(loud) < 0.9, f"max {max(loud):.3f}")


# ---------------------------------------------------------------- when it applies
# The gain must follow the state: on while waiting for a request, off otherwise,
# so the barge detector never sees a boosted signal while it is talking.
from archy.assistant import Assistant

class FakeMic:
    def __init__(self): self.focused = None
    def set_focus(self, on): self.focused = on
    def drain(self): pass

h = Assistant.__new__(Assistant)
h.mic = FakeMic()
h.hud = None
h.state = "booting"

for state, expect in [("listening", True), ("thinking", False),
                      ("speaking", False), ("idle", False), ("listening", True)]:
    h.set_state(state)
    check(f"focus is {expect} in the {state} state", h.mic.focused is expect,
          f"focus={h.mic.focused}")

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
