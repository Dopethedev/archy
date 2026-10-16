"""Drive the real _pump with synthetic frames.

This is the one part that cannot be verified without a human at the mic, so
it is exercised directly: does it wake, does it end the utterance when the
speech stops, and does it give up when nothing is said.
"""
import asyncio, queue, sys
import numpy as np

sys.path.insert(0, "/home/dope/jarvis/src")
from jarvis import config
from jarvis.assistant import Assistant
from jarvis.assistant import blip

print("config: INPUT_BLOCK=%d WAKE_FRAME=%d END_SILENCE_MS=%d" %
      (config.INPUT_BLOCK, config.WAKE_FRAME, config.END_SILENCE_MS))
FRAME_MS = 1000.0 * config.INPUT_BLOCK / config.SAMPLE_RATE


class FakeWake:
    def __init__(self):
        self.fire_at = None
        self.pushes = 0
    def push(self, frame):
        self.pushes += 1
        fire = self.fire_at is not None and self.pushes >= self.fire_at
        if fire:
            self.fire_at = None
        return fire
    def reset(self):
        pass


class Harness(Assistant):
    """The real Assistant with the heavy and audible parts swapped out."""
    def __init__(self):
        self.hud = None
        self.state = "booting"
        self.mic = type("M", (), {"frames": queue.Queue(), "level": 0.0, "drain": lambda s: None})()
        self.player = type("P", (), {"level": 0.0, "write": lambda s, a: None, "playing": lambda s: False})()
        self.wake = FakeWake()
        self._typed = queue.Queue()
        self._stopping = False
        self._vad = None
        self.trace = []
        self.ended = None
        self.score = 0.1
    def speech_score(self, frame):
        return 0.9 if int(frame[0]) == 900 else 0.1
    def log(self, who, text):
        self.trace.append(f"{who}: {text}")
    def set_state(self, s):
        self.state = s
    def _publish_levels(self):
        pass
    def _start_utterance(self, audio, speech_ms):
        # call the real one, so its too-short guard is what is being tested
        Assistant._start_utterance(self, audio, speech_ms)
        if self.state == "thinking":      # the real code accepted it
            self.ended = (len(audio), speech_ms)
            self.set_state("listening")   # release the state for the next case
    async def _process(self, audio=None, text=None):
        pass                              # replacing this is the point
    def _back_to_idle(self):
        self.trace.append("sys: idle")
        self.set_state("idle")


async def scenario(name, frames_plan, expect_end, timeout_s=None):
    if timeout_s is not None:
        config.LISTEN_TIMEOUT_S = timeout_s
    h = Harness()
    h.state = "idle"

    async def feed():
        for kind in frames_plan:
            if kind == "wake":
                h.wake.fire_at = h.wake.pushes + 1
                continue
            if kind == "wait":
                await asyncio.sleep(0.35)
                continue
            # the level rides on the frame itself, so it is correct whenever
            # the pump actually gets around to processing it
            level = 900 if kind == "speech" else 100
            h.mic.frames.put(np.full(config.INPUT_BLOCK, level, dtype=np.int16))
            await asyncio.sleep(0)

    task = asyncio.create_task(h._pump())
    await feed()
    await asyncio.sleep(0.3)
    h._stopping = True
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    ok = True
    if expect_end == "utterance":
        ok = h.ended is not None and h.ended[1] >= config.MIN_SPEECH_MS
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}: ended={h.ended} "
              f"(speech {h.ended[1] if h.ended else 0:.0f}ms)")
    elif expect_end == "timeout":
        ok = h.ended is None and "sys: idle" in h.trace
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}: ended={h.ended} trace={h.trace}")
    else:
        ok = h.ended is None
        print(f"  [{'OK  ' if ok else 'FAIL'}] {name}: ended={h.ended}")
    if not ok:
        print(f"       full trace: {h.trace}")
    return ok


async def main():
    results = []
    results.append(await scenario(
        "wake, 600ms speech, then 1.2s silence -> utterance ends",
        ["quiet", "quiet", "wake", "speech"] + ["speech"] * 19 + ["quiet"] * 40,
        "utterance"))
    results.append(await scenario(
        "wake, then nothing said -> gives up",
        ["wake", "quiet"] + ["wait", "quiet"] * 3,
        "timeout", timeout_s=0.7))
    results.append(await scenario(
        "no wake -> never records",
        ["quiet"] * 60, "none"))
    results.append(await scenario(
        "wake, 5 frames of speech -> too short, ignored",
        ["wake"] + ["speech"] * 5 + ["quiet"] * 40, "none"))
    print(f"\n{'ALL PASS' if all(results) else 'FAILURES'}")
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
