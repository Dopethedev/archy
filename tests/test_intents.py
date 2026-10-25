"""The brain chain, the local intents, and the briefing turn.

The model chain is tested against a fake pi process, never the real relay. The
turn wiring follows the harness style of test_sweep: an Assistant built with
__new__, its collaborators stubbed, patching names where they are looked up.
"""
import asyncio
import io
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import jarvis.assistant as jarvis_assistant
import jarvis.brain as brain_mod
import jarvis.reminders as reminders
import jarvis.briefing as briefing
from jarvis import config, privacy
from jarvis.assistant import Assistant
from jarvis.brain import PiBrain

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


print("-- the chain --")
# Pinned for the same reason: the owner's config.local.json can force offline-only
# or point the chain at a local model, and these assertions are about the relay
# chain and the command line it produces.
real_models = config.PI_MODELS
saved_mode, saved_chain = config.PI_CHAIN_MODE, config.PI_MODELS_OFFLINE
config.PI_CHAIN_MODE, config.PI_MODELS_OFFLINE = "auto", ()
config.PI_MODELS = ("model/a-first", "model/a-second", "model/a-third")
brain = PiBrain()
check("it starts at the head", brain.model_name() == "model/a-first")
check("advance names the next", brain.advance_model() == "model/a-second")
check("advance again", brain.advance_model() == "model/a-third")
check("it wraps around", brain.advance_model() == "model/a-first")

print("-- a dead relay puts the next model on the command line --")
class FakeProc:
    def __init__(self, args, **kwargs):
        FakeProc.seen_args = args
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self.pid = 4242
    def poll(self): return None
    def kill(self): pass
    def terminate(self): pass
    def wait(self, timeout=None): return 0

FakeProc.seen_args = None
real_popen = brain_mod.subprocess.Popen
brain_mod.subprocess.Popen = FakeProc
try:
    brain = PiBrain()
    brain.model_index = 1
    asyncio.run(brain.start())
    args = FakeProc.seen_args
    check("the chain entry chosen is the one on the command line",
          "--model" in args and args[args.index("--model") + 1] == "model/a-second",
          str(args[-2:]))
finally:
    brain_mod.subprocess.Popen = real_popen
config.PI_MODELS = real_models
config.PI_CHAIN_MODE, config.PI_MODELS_OFFLINE = saved_mode, saved_chain

print("-- turn wiring: local intents and the briefing --")


class FakeBrain:
    def __init__(self):
        self.seen = []
    def model_name(self):
        return "some-relay/large-model"      # a remote model, so the switches apply
    async def ask(self, text, images=None):
        self.seen.append((text, images))
        yield "done", "as you wish."


class Harness:
    def __init__(self):
        # The constructor is not run here, so anything the assistant sets up in it
        # has to be provided, or a turn fails on an attribute that never existed.
        self._privacy_confirm = privacy.Confirmation()
        self.spoken = []
        self.published = []
        self.log_lines = []
        self.brain = FakeBrain()
        self.speaker = self
        self.player = type("P", (), {"wait_drained": lambda s, *a, **k: None,
                                     "write": lambda s, x: None})()
        self.mic_enabled = True
        self.state = "thinking"
        self.hud = self
        self._speaking_since = 0.0
        self.turn_latency = 0.0
        self._stopping = False
    def say_stream(self, deltas, on_first, cancel):
        for item in deltas:
            self.spoken.append(item)
    def say(self, text):
        self.spoken.append(text)
        return True
    _theme_intent = staticmethod(Assistant._theme_intent)
    def publish(self, event):
        self.published.append(event)
    def log(self, who, text):
        self.log_lines.append((who, text))
    def _publish_heard(self, text, partial=False):
        pass
    def set_state(self, s):
        self.state = s
    def _back_to_idle(self):
        self.set_state("idle")


async def run_turn(text):
    h = Harness()
    await Assistant._process(h, text=text)
    return h


async def main():
    tmp = Path(tempfile.mkdtemp())
    config.STATE_DIR = tmp
    reminders.PATH = tmp / "reminders.json"

    h = await run_turn("set a timer for 5 minutes")
    check("a timer is confirmed without the model",
          h.brain.seen == [] and any("5 minutes" in s for s in h.spoken),
          str(h.spoken[:2]))

    reminders.PATH.unlink(missing_ok=True)      # the timer from the last turn is not part of this one
    h = await run_turn("remind me in 1 minute to check the oven")
    check("a reminder is stored",
          any("oven" in s for s in h.spoken)
          and len(reminders.pending()) == 1, str(reminders.pending()))

    h = await run_turn("what reminders do i have")
    check("a query reads the pending list",
          any("check the oven" in s.lower() for s in h.spoken), str(h.spoken[:2]))

    h = await run_turn("cancel my reminders")
    check("a cancel clears the store",
          any("cleared" in s.lower() for s in h.spoken) and reminders.pending() == [],
          str(h.spoken[:1]))

    # and a clearance can be taken back from a turn, without the model
    h = await run_turn("undo")
    check("undo brings the cleared reminders back",
          any("brought back" in s.lower() for s in h.spoken) and reminders.pending() != [],
          str(h.spoken[:1]))
    check("and the model was never asked", h.brain.seen == [], str(h.brain.seen[:1]))

    h = await run_turn("combat mode")
    check("a theme is published to the dashboard",
          any(e.get("type") == "theme" and e.get("value") == "combat"
              for e in h.published))
    check("and confirmed in a voice of its own",
          any("Combat mode" in s for s in h.spoken))

    h = await run_turn("the combat in that game was intense")
    check("a passing mention of combat changes nothing",
          not any(e.get("type") == "theme" for e in h.published)
          and h.brain.seen != [])

    h = await run_turn("what is the best combat mode in this game")
    check("a question about a game's combat mode changes nothing",
          not any(e.get("type") == "theme" for e in h.published)
          and h.brain.seen != [])

    h = await run_turn("switch theme to stealth")
    check("theme-to-name shape works",
          any(e.get("type") == "theme" and e.get("value") == "stealth"
              for e in h.published))

    h = await run_turn("switch back to standard mode")
    check("standard is reachable by voice",
          any(e.get("type") == "theme" and e.get("value") == "standard"
              for e in h.published))

    real_gather = briefing.gather
    briefing.gather = lambda include_weather=True: ("LOCAL FACTS.\nTime: 09:00.\nWeather in "
                               "Berlin: 24 degrees, clear.\n")
    try:
        h = await run_turn("brief me")
        check("the briefing carries the facts to the model",
              h.brain.seen and "LOCAL FACTS" in h.brain.seen[0][0]
              and "Berlin" in h.brain.seen[0][0])
        # and it is told which numbers to trust: on a long session the model also
        # has yesterday's briefing in front of it, and it quoted those instead
        check("the facts are marked as current",
              h.brain.seen and "read just now" in h.brain.seen[0][0]
              and "out of date" in h.brain.seen[0][0],
              f"prompt said: {h.brain.seen[0][0][-90:]!r}" if h.brain.seen else "no prompt")
    finally:
        briefing.gather = real_gather

    print("-- a due reminder is spoken, whatever the state was --")
    real_check_s = config.REMINDER_CHECK_S
    config.REMINDER_CHECK_S = 0.05
    reminders.PATH.unlink(missing_ok=True)
    reminders.add("Check the oven", -1.0)          # overdue the moment it exists
    try:
        h = Harness()
        h.state = "idle"
        task = asyncio.create_task(Assistant._reminder_loop(h))
        for _ in range(100):
            if any("Reminder: Check the oven" in s for s in h.spoken):
                break
            await asyncio.sleep(0.05)
        task.cancel()
        check("the reminder is spoken",
              any("Reminder: Check the oven" in s for s in h.spoken), str(h.spoken))
        check("it announces itself and returns to idle",
              "speaking" in h.state or h.state == "idle")
        check("and leaves the store empty", reminders.pending() == [])
    finally:
        config.REMINDER_CHECK_S = real_check_s

    print()
    print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
