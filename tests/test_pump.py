"""Drive the real _pump with synthetic frames.

This is the one part that cannot be verified without a human at the mic, so
it is exercised directly: does it wake, does it end the utterance when the
speech stops, and does it give up when nothing is said.
"""
from pathlib import Path
import asyncio, queue, sys, threading, time
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis import config
# this file tests the trained-detector path; speech mode is covered by
# tests/test_wake_phrase.py
config.WAKE_MODE = "model"
from jarvis.assistant import Assistant
from jarvis.wake import SpeechWake
from jarvis.assistant import blip

print("config: INPUT_BLOCK=%d WAKE_FRAME=%d END_SILENCE_MS=%d" %
      (config.INPUT_BLOCK, config.WAKE_FRAME, config.END_SILENCE_MS))
FRAME_MS = 1000.0 * config.INPUT_BLOCK / config.SAMPLE_RATE


class FakeWake:
    def __init__(self):
        self.fire_at = None
        self.pushes = 0
        self.score = 0.99
    def push(self, frame, threshold=None):
        self.pushes += 1
        fire = self.fire_at is not None and self.pushes >= self.fire_at
        if not fire:
            return False
        self.fire_at = None
        # honour the threshold actually in force, as the real detector does
        limit = config.WAKE_THRESHOLD if threshold is None else threshold
        return self.score >= limit
    def reset(self):
        pass


class FakeTranscriber:
    def __init__(self):
        self.calls = 0
    def transcribe(self, audio):
        self.calls += 1
        return "speculative words"


class Harness(Assistant):
    """The real Assistant with the heavy and audible parts swapped out."""
    def __init__(self):
        self.hud = None
        self.state = "booting"
        self.mic = type("M", (), {"frames": queue.Queue(), "level": 0.0, "drain": lambda s: None, "set_focus": lambda s, on: None,
                                    "start": lambda s: None, "stop": lambda s: None})()
        self.flushed = 0
        self.aborted = 0
        player = type("P", (), {"level": 0.0, "write": lambda s, a: None, "playing": lambda s: False,
                                "flushed": 0,
                                "flush": lambda s: setattr(s, "flushed", s.flushed + 1)})()
        self.player = player
        brain = type("B", (), {"aborted": 0,
                               "abort": lambda s: setattr(s, "aborted", s.aborted + 1)})()
        self.brain = brain
        self.wake = FakeWake()
        self.speech_wake = SpeechWake()
        self.wake_audio = []
        self.transcriber = FakeTranscriber()
        self.processed = []
        self._typed = queue.Queue()
        self._stopping = False
        self._vad = None
        self._cancel = threading.Event()
        self._speaking_since = 0.0
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
    def _start_utterance(self, audio, speech_ms, transcript=None):
        # call the real one, so its too-short guard is what is being tested
        Assistant._start_utterance(self, audio, speech_ms, transcript)
        if self.state == "thinking":      # the real code accepted it
            self.ended = (len(audio), speech_ms)
            # deliberately left in "thinking": the pump drains audio in that
            # state, which is what stops one recording being handled twice
    async def _process(self, audio=None, text=None):
        self.processed.append(text)               # None means it transcribed normally
    async def _check_wake(self, audio, transcript):
        self.wake_audio.append(len(audio))
        self._back_to_idle()
    def _back_to_idle(self):
        self.trace.append("sys: idle")
        self.set_state("idle")


class TurnHarness(Harness):
    """Counts how many turns are running at once.

    The override behaves like the real _process: it installs its own cancel
    token for the turn and stops as soon as that token is set, which is what an
    interruption does.
    """
    def __init__(self):
        super().__init__()
        self.active = 0
        self.peak = 0
        self.turns = []
        self.durations = []
    async def _process(self, audio=None, text=None):
        # like the real one: claim a fresh token for this turn, which is what
        # makes an interruption unable to touch the turn that replaces it
        token = threading.Event()
        self._cancel = token
        started = time.monotonic()
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.turns.append(text)
        try:
            for _ in range(80):
                await asyncio.sleep(0.01)
                if token.is_set():
                    return
        finally:
            self.durations.append(time.monotonic() - started)
            self.active -= 1


async def scenario_typed_while_replying(name):
    """Typing into the dashboard while a reply is playing.

    This used to start a second turn alongside the first: two voices, two
    answers, and a state neither of them owned.
    """
    h = TurnHarness()
    h.state = "speaking"
    h._speaking_since = 0.0
    first = asyncio.create_task(h._process(text="a spoken request"))
    await asyncio.sleep(0.05)

    task = asyncio.create_task(h._pump())
    await asyncio.sleep(0.05)
    h._typed.put("a typed request")
    await asyncio.sleep(0.4)

    peak, turns, aborted = h.peak, h.turns, h.brain.aborted
    still_running = h.active
    durations = list(h.durations)
    h._stopping = True
    await asyncio.sleep(0.05)
    for t in (first, task):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    # what matters is not that the overlap is zero for a few milliseconds while
    # the first turn notices, but that it is cut short and one turn remains
    ok = (aborted == 1 and still_running == 1 and turns == ["a spoken request", "a typed request"]
          and durations and durations[0] < 0.3)
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}: interrupted={aborted} "
          f"turns_left={still_running} cut_short={durations[0] if durations else -1:.2f}s "
          f"(a whole turn is 0.80s) turns={turns}")
    return ok


async def scenario_speech_while_thinking(name, enabled, frames=16, expect_interrupt=True):
    """Talking while it is still thinking redirects it, instead of waiting.

    Deliberately harder than interrupting a reply: nothing is audible while it
    thinks, so a stray word would silently swallow the answer.
    """
    config.BARGE_ON_THINKING = enabled
    h = Harness()
    h.state = "thinking"

    async def feed():
        for _ in range(frames):
            h.mic.frames.put(np.full(config.INPUT_BLOCK, 900, dtype=np.int16))
            await asyncio.sleep(0)

    task = asyncio.create_task(h._pump())
    await feed()
    await asyncio.sleep(0.2)
    state, aborted = h.state, h.brain.aborted
    h._stopping = True
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    heard = state == "listening" and aborted >= 1
    ok = heard if (enabled and expect_interrupt) else (state == "thinking" and aborted == 0)
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}: state={state} interrupted={aborted} "
          f"after {frames * 30}ms of speech (BARGE_ON_THINKING={enabled})")
    config.BARGE_ON_THINKING = True
    return ok


async def scenario_too_short_keeps_listening(name):
    """A blip while listening must not end the turn outright.

    The tail of the wake phrase, or a cough, used to be enough to send it back
    to idle, so a request that started a moment later was simply not heard.
    """
    h = Harness()
    h.state = "idle"

    async def feed():
        h.wake.fire_at = h.wake.pushes + 1
        h.mic.frames.put(np.full(config.INPUT_BLOCK, 100, dtype=np.int16))
        await asyncio.sleep(0.1)                 # wake arms listening
        h.mic.frames.put(np.full(config.INPUT_BLOCK, 900, dtype=np.int16))   # one movement
        await asyncio.sleep(0.05)
        for _ in range(30):                      # then silence ends the utterance
            h.mic.frames.put(np.full(config.INPUT_BLOCK, 100, dtype=np.int16))
            await asyncio.sleep(0)
        await asyncio.sleep(0.15)

    task = asyncio.create_task(h._pump())
    await feed()
    state = h.state
    h._stopping = True
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    ok = state == "listening" and h.ended is None
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}: state={state} (still waiting for a request)")
    return ok


async def scenario_armed_while_queue_is_empty(name):
    """The give-up deadline is measured from when listening was armed.

    A task arms listening (the beep has just played) and for a moment no audio
    frame is queued. Judged against a stale timestamp, the deadline fired in the
    same instant and the reply was "nothing said, going quiet" one line after
    the wake phrase: it beeped, listened, and stopped.
    """
    config.LISTEN_TIMEOUT_S = 0.8
    h = Harness()
    h.state = "idle"
    task = asyncio.create_task(h._pump())
    await asyncio.sleep(0.05)

    h.set_state("listening")          # exactly what the beep path does
    await asyncio.sleep(0.35)         # well inside the deadline
    early = sum("nothing said" in t for t in h.trace)
    await asyncio.sleep(0.75)         # now past it
    late = sum("nothing said" in t for t in h.trace)

    h._stopping = True
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    ok = early == 0 and late >= 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}: gave up {early} times inside the "
          f"deadline, {late} after it")
    config.LISTEN_TIMEOUT_S = 12.0
    return ok


async def scenario_muted_is_inert(name):
    """With the microphone off, audio must not be able to do anything at all.

    This is the point of the switch: the same frames that would wake it, be
    heard as a request, and interrupt a reply, must reach none of those.
    """
    h = Harness()
    h.mic_enabled = False
    h.state = "muted"
    h.wake.fire_at = 1                    # the detector would fire if asked
    task = asyncio.create_task(h._pump())
    for _ in range(25):
        h.mic.frames.put(np.full(config.INPUT_BLOCK, 900, dtype=np.int16))
        await asyncio.sleep(0.01)
    seen = h.state
    h._stopping = True
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    ok = seen == "muted" and h.ended is None and not any("wake" in t for t in h.trace)
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}: state={seen}, "
          f"turns={len(h.ended is not None and [1] or [])}, trace={h.trace}")
    return ok


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


async def scenario_barge(name, speaking_ago, expect_interrupt, wake_score=0.99,
                         frame_value=100, frames=20, then_silence=0, wake_fires=True):
    """While it is speaking, what does and does not cut it off?

    frame_value 900 reads as speech to the VAD, 100 reads as quiet, and
    wake_fires decides whether the wake word also triggers. That lets the two
    interrupt paths be tested separately, since either one can cut it off.
    """
    h = Harness()
    h.state = "speaking"
    h._speaking_since = asyncio.get_running_loop().time() - speaking_ago
    h.wake.fire_at = h.wake.pushes + 1 if wake_fires else None
    h.wake.score = wake_score

    task = asyncio.create_task(h._pump())
    for _ in range(frames):
        h.mic.frames.put(np.full(config.INPUT_BLOCK, frame_value, dtype=np.int16))
        await asyncio.sleep(0)
    await asyncio.sleep(0.1)
    was_listening = h.state == "listening"
    for _ in range(then_silence):
        h.mic.frames.put(np.full(config.INPUT_BLOCK, 100, dtype=np.int16))
        await asyncio.sleep(0)
    await asyncio.sleep(0.1)
    h._stopping = True
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    interrupted = was_listening and h.player.flushed > 0 and h.brain.aborted > 0
    ok = interrupted == expect_interrupt
    detail = (f"state={h.state} flushed={h.player.flushed} aborted={h.brain.aborted} "
              f"interrupted={interrupted} expected={expect_interrupt}")
    if then_silence:
        # if the triggering audio was kept, the utterance can now end on silence;
        # if it was dropped, nothing was ever recorded and this stays None
        kept = h.ended is not None
        ok = ok and kept
        detail += f" utterance_kept={kept}"
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}: {detail}")
    return ok


async def scenario_speculation(name, resume_speaking, expect_reused):
    """Is the paused-time transcript reused, and thrown away if he talks again?"""
    h = Harness()
    h.state = "idle"
    h.wake.fire_at = h.wake.pushes + 1

    async def feed(value, count):
        for _ in range(count):
            h.mic.frames.put(np.full(config.INPUT_BLOCK, value, dtype=np.int16))
            await asyncio.sleep(0)

    task = asyncio.create_task(h._pump())
    await feed(900, 20)                        # speech
    # enough silence to trigger speculation, not enough to end the utterance
    await feed(100, int(config.SPECULATE_AT_MS / 30) + 3)
    await asyncio.sleep(0.15)                  # let it transcribe in the background
    if resume_speaking:
        await feed(900, 10)                    # he speaks again: snapshot is stale
    await feed(100, 40)                        # silence, this ends the utterance
    await asyncio.sleep(0.35)

    h._stopping = True
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    reused = bool(h.processed) and h.processed[-1] == "speculative words"
    ok = reused == expect_reused
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}: reused={reused} expected={expect_reused} "
          f"transcriber_calls={h.transcriber.calls} handled={h.processed}")
    return ok


async def scenario_speech_idle(name, plan, expect_wake_check, expect_timeout_logs=0):
    """Speech mode: idle records, but idle has no deadline.

    Treating idle as "listening with a timeout" produced a runaway that logged
    once per queued frame, so this pins the correct behaviour down.
    """
    config.WAKE_MODE = "speech"
    h = Harness()
    h.state = "idle"
    h.wake = None                       # nothing to detect with in this mode

    task = asyncio.create_task(h._pump())
    for kind in plan:
        value = 900 if kind == "speech" else 100
        h.mic.frames.put(np.full(config.INPUT_BLOCK, value, dtype=np.int16))
        await asyncio.sleep(0)
    await asyncio.sleep(0.3)
    h._stopping = True
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    spam = sum("nothing said" in line for line in h.trace)
    checked = bool(h.wake_audio)          # did the wake phrase get looked for
    ok = spam == expect_timeout_logs and checked == expect_wake_check
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}: timeout_logs={spam} "
          f"wake_checked={checked} (expected logs={expect_timeout_logs}, "
          f"checked={expect_wake_check})")
    config.WAKE_MODE = "model"
    return ok


async def scenario_endless_noise(name, seconds, expect_max_s):
    """Continuous sound must not become a queue of long recordings.

    With nothing to end the utterance, the wake path recorded up to the general
    limit and transcribed all of it, and several of those ran at once, which put
    the machine at nearly three cores for minutes with nothing to show for it.
    """
    config.WAKE_MODE = "speech"
    h = Harness()
    h.state = "idle"
    h.wake = None

    frames = int(seconds * 1000 / 30)          # 30 ms frames of unbroken "speech"
    task = asyncio.create_task(h._pump())
    for _ in range(frames):
        h.mic.frames.put(np.full(config.INPUT_BLOCK, 900, dtype=np.int16))
        await asyncio.sleep(0)
    await asyncio.sleep(0.3)
    h._stopping = True
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    longest = max(h.wake_audio) / config.SAMPLE_RATE if h.wake_audio else 0.0
    dropped = sum("dropping it" in line for line in h.trace)
    # the bound is the cap: a recording may end at it and be transcribed (that is
    # how a wake phrase inside continuous room speech gets heard), but it must
    # never grow past it, and the number of transcriptions must track the time,
    # not the frame count
    capped = seconds / config.WAKE_MAX_UTTERANCE_S + 1
    ok = longest <= expect_max_s and len(h.wake_audio) <= capped and dropped == 0
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}: {len(h.wake_audio)} recordings, "
          f"longest {longest:.1f}s (limit {expect_max_s}s, at most {capped:.0f})")
    config.WAKE_MODE = "model"
    return ok


async def main():
    results = []
    results.append(await scenario(
        "wake, 600ms speech, then 1.2s silence -> utterance ends",
        ["quiet", "quiet", "wake", "speech"] + ["speech"] * 19 + ["quiet"] * 40,
        "utterance"))
    results.append(await scenario(
        "wake, then nothing said -> gives up",        # the extra frames matter: the timeout is only noticed when a frame
        # arrives, so one quiet frame after the wait can be missed under load
        ["wake", "quiet"] + ["wait", "quiet"] * 3 + ["quiet"] * 12,
        "timeout", timeout_s=0.7))
    results.append(await scenario(
        "no wake -> never records",
        ["quiet"] * 60, "none"))
    results.append(await scenario(
        "wake, 5 frames of speech -> too short, ignored",
        ["wake"] + ["speech"] * 5 + ["quiet"] * 40, "none"))

    print("  -- interruption --")
    results.append(await scenario_barge(
        "wake word after the grace period -> cuts it off", 1.5, True))
    results.append(await scenario_barge(
        "wake word during the grace period -> allowed through", 0.1, False))
    results.append(await scenario_barge(
        "weak wake match below the strict threshold -> ignored", 1.5, False, wake_score=0.4))
    results.append(await scenario_barge(
        "just talking over it -> cuts it off, and keeps the first word",
        1.5, True, frame_value=900, frames=14, then_silence=25, wake_fires=False))
    results.append(await scenario_barge(
        "a brief blip of speech -> not enough to interrupt",
        1.5, False, frame_value=900, frames=2, wake_fires=False))

    print("  -- transcription during the pause --")
    results.append(await scenario_speculation(
        "silence after speech -> the ready transcript is used", False, True))
    results.append(await scenario_speculation(
        "speaks again -> the stale transcript is discarded", True, False))

    print("  -- speech mode --")
    results.append(await scenario_speech_idle(
        "idle with only silence -> no timeout spam", ["quiet"] * 40, False))
    results.append(await scenario_speech_idle(
        "idle hears speech -> the wake phrase is looked for",
        ["speech"] * 20 + ["quiet"] * 40, True))
    results.append(await scenario_speech_idle(
        "idle hears a blip -> not worth transcribing",
        ["speech"] * 4 + ["quiet"] * 40, False))
    results.append(await scenario_endless_noise(
        "unbroken noise for 60s -> recordings stay short", 60, config.WAKE_MAX_UTTERANCE_S + 0.5))
    results.append(await scenario_muted_is_inert(
        "microphone off -> loud speech does nothing"))
    results.append(await scenario_armed_while_queue_is_empty(
        "armed while no audio is queued -> deadline still measured from the arm"))
    results.append(await scenario_too_short_keeps_listening(
        "a blip while listening -> keeps waiting instead of giving up"))
    results.append(await scenario_typed_while_replying(
        "typing while it talks -> still one turn at a time"))
    results.append(await scenario_speech_while_thinking(
        "talking over it while it thinks -> redirects it", True, frames=16))
    results.append(await scenario_speech_while_thinking(
        "a brief word while it thinks -> the answer survives", True,
        frames=6, expect_interrupt=False))
    results.append(await scenario_speech_while_thinking(
        "that can be turned off", False, frames=16))

    print(f"\n{'ALL PASS' if all(results) else 'FAILURES'}")
    return 0 if all(results) else 1


sys.exit(asyncio.run(main()))
