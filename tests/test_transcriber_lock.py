"""One transcription at a time, and never an unbounded wait for one.

The model is not meant to run concurrently. Several overlapping transcriptions
on a six-thread model thrash the machine instead of finishing sooner: this was
measuring nearly three cores busy for minutes with no output to show for it.

Serialising them created the second half of the problem. A pass that is slow,
or cancelled but still running in its thread, holds the lock, and every caller
after it waits behind it: on a real run the assistant said nothing at all for
seventy three seconds and then produced a transcript of three wake phrases said
during that time. Nothing in the log, nothing on the screen, nothing said.
"""
from pathlib import Path
import asyncio
import sys, threading, time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np
from archy import config
from archy.stt import Transcriber
from archy.assistant import Assistant, RecognitionTooSlow, words_heard

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

# the lock, asked whether it is taken: speculation is only worth starting when
# it is free, because a second pass behind the first waits just as long and
# makes the turn wait for both
started = threading.Thread(target=t.transcribe, args=(audio,))
started.start()
for _ in range(50):                       # until the worker is inside the model
    if t.busy():
        break
    time.sleep(0.01)
check("the lock reports a pass in flight", t.busy() is True)
started.join()
check("and reports the recogniser free again", t.busy() is False)


class StallingTranscriber:
    """Every pass outlives the deadline, and holds the recogniser doing it."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.calls = 0

    def busy(self):
        return False

    def transcribe(self, audio):
        self.calls += 1
        time.sleep(self.seconds)
        return "the words that were said"


class QuickTranscriber(StallingTranscriber):
    def __init__(self):
        super().__init__(0.0)


config.TRANSCRIBE_TIMEOUT_S = 0.25                    # the deadline under test
stalling = type("V", (), {"transcriber": StallingTranscriber(1.5)})()
quick = type("V", (), {"transcriber": QuickTranscriber()})()


async def deadline_cases():
    start = time.monotonic()
    raised = None
    try:
        words = await words_heard(stalling, audio, None)
    except RecognitionTooSlow as exc:
        raised, words = exc, None
    took = time.monotonic() - start
    check("a stalled pass gives up", raised is not None, str(raised))
    check("at the deadline, not when it finishes", took < 0.7, f"{took:.2f}s for a 1.5s pass")

    # waiting for a speculation the turn adopts is the same wait, and it is the
    # one that cost seventy three seconds
    pending = asyncio.get_running_loop().run_in_executor(
        None, stalling.transcriber.transcribe, audio)
    start = time.monotonic()
    raised = None
    try:
        await words_heard(stalling, audio, None, asyncio.ensure_future(pending))
    except RecognitionTooSlow as exc:
        raised = exc
    took = time.monotonic() - start
    check("an adopted speculation is bounded too",
          raised is not None and took < 0.7, f"{took:.2f}s")

    # and the deadline is not in the way of the normal case
    start = time.monotonic()
    words = await words_heard(quick, audio, None)
    took = time.monotonic() - start
    check("a pass that finishes is unaffected",
          words == "the words that were said", f"{took:.3f}s, words={words!r}")


class MuteHarness:
    """The real _process with everything audible and heavy stubbed out."""

    def __init__(self, transcriber):
        self.transcriber = transcriber
        self.state = "idle"
        self._cancel = threading.Event()
        self.said = []
        self.trace = []
        self.speaker = type("S", (), {"say": lambda s, text: self.said.append(text)})()
        self.player = type("P", (), {"wait_drained": lambda s, t=0: True})()

    def log(self, who, text):
        self.trace.append(f"{who}: {text}")

    def _publish_heard(self, *args, **kwargs):
        pass

    def _back_to_idle(self):
        self.state = "idle"
        self.trace.append("sys: idle")


async def mute_cases():
    h = MuteHarness(StallingTranscriber(1.5))
    start = time.monotonic()
    await Assistant._process(h, audio=audio)
    took = time.monotonic() - start
    check("a stalled recogniser is answered, not waited on",
          took < 0.7 and bool(h.said), f"{took:.2f}s said={h.said}")
    check("and the turn ends instead of hanging",
          h.state == "idle" and any("idle" in line for line in h.trace),
          f"state={h.state}")
    check("and the wait is in the record",
          any("recogniser" in line for line in h.trace), str(h.trace[-2:]))


asyncio.run(deadline_cases())
asyncio.run(mute_cases())

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
