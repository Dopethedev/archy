"""A turn must not run a slow thing on the thread that reads the microphone.

The voice loop is one thread. Anything blocking inside it stops the assistant
hearing: the wake phrase, the barge-in, the next sentence. Some of those things are
slow by nature (the screen, the sweep, the briefing) and are handed to another
thread already. The compositor was not, on the reasoning that it answers in about
five milliseconds here. Five milliseconds is not the risk; a compositor that has
stopped answering is, and then the cost is the turn and everything behind it.

Two ways of holding that: a static one that reads _process and objects to any of
these helpers being called straight from it, and a live one that hangs the
compositor and watches whether the loop keeps ticking while it does.
"""
from pathlib import Path
import ast
import asyncio
import re
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import os, tempfile as _tf
os.environ["JARVIS_SESSION_DIR"] = _tf.mkdtemp(prefix="jarvis-test-session-")
os.environ.setdefault("JARVIS_MEMORY_FILE", "/tmp/jarvis-test-memory.json")

from jarvis import config  # noqa: E402
from jarvis import desktop
from jarvis import privacy
from jarvis import status as status_mod
from jarvis import assistant as jarvis_assistant
from jarvis.assistant import Assistant

failures = 0
def check(label, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:52s} {detail}")


# helpers that can block for as long as a program takes, and so may only be
# reached through asyncio.to_thread. Named by the attribute the assistant calls.
BLOCKING = {
    "windows", "focus", "close_focused",   # the compositor
    "capture",                             # the screen
    "run", "gather",                       # the sweep, the briefing
}


def blocking_calls_outside_threads(source: str) -> list[str]:
    """Names of blocking helpers called from _process without to_thread around them."""
    tree = ast.parse(source)
    # a turn is a coroutine, so it is an AsyncFunctionDef and not a FunctionDef,
    # which is the kind of detail that makes a reader find nothing and pass
    process = next(node for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == "_process")

    threaded = [node for node in ast.walk(process)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "to_thread"]
    inside = {id(node) for call in threaded for node in ast.walk(call)}

    offenders = []
    for node in ast.walk(process):
        if not isinstance(node, ast.Call) or id(node) in inside:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else "")
        if name in BLOCKING:
            offenders.append(f"{name}() on line {node.lineno}")
    return offenders


source = Path(jarvis_assistant.__file__).read_text()
offenders = blocking_calls_outside_threads(source)
check("no blocking helper is called straight from a turn",
      not offenders, ", ".join(offenders) or "every one is on another thread")

# and the reader has to be able to see one, or it proves nothing
borrowed = source.replace("await asyncio.to_thread(desktop_mod.windows)",
                          "desktop_mod.windows()", 1)
check("and the reader spots one when it is there",
      bool(blocking_calls_outside_threads(borrowed)),
      ", ".join(blocking_calls_outside_threads(borrowed)) or "found nothing")


class Harness:
    """Enough of an Assistant for _process to run a turn with stubs."""

    def __init__(self):
        self._privacy_confirm = privacy.Confirmation()
        self.status = status_mod.Feed()
        self.spoken: list[str] = []
        self.brain = FakeBrain()
        self.hud = None
        self.mic_enabled = True
        self.state = "thinking"
        self.player = type("P", (), {"wait_drained": lambda s, t=None: None})()

        def say_stream(deltas, on_spoken, _cancel):
            said = [d for d in deltas if d is not None]
            self.spoken.extend(said)
            if on_spoken and said:
                on_spoken()

        self.speaker = type("S", (), {
            "say_stream": staticmethod(say_stream),
            "say": staticmethod(lambda text: self.spoken.append(text))})()
        self._theme_intent = Assistant._theme_intent

    def log(self, who, text):
        pass
    def _publish_heard(self, text, partial=False):
        pass
    def set_state(self, s):
        self.state = s
    def _back_to_idle(self):
        self.set_state("idle")


class FakeBrain:
    def model_name(self):
        return "some-relay/large-model"

    async def ask(self, text, images=None):
        yield "done", "the model answered"


CALLER = []


def hung_desktop(seconds: float, answer):
    """A compositor that has stopped answering, and the thread it was asked on."""
    def call(*args, **kwargs):
        CALLER.append(threading.current_thread().name)
        time.sleep(seconds)
        return answer
    return call


async def run_turn(text: str, seconds: float, patch: dict):
    """Run one turn while a slow compositor is hung, counting the loop's ticks.

    The ticker is the microphone: if the loop is free it keeps running, and the
    count is the measure. It is started before the turn and stopped after, so a
    turn that blocks shows up as ticks that never happened rather than as a number
    nobody can read.
    """
    CALLER.clear()
    harness = Harness()
    saved = {name: getattr(desktop, name) for name in patch}
    for name, replacement in patch.items():
        setattr(desktop, name, replacement)

    ticks = 0
    stop = threading.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.02)

    tick_task = asyncio.create_task(ticker())
    try:
        await asyncio.sleep(0.05)                       # let the ticker start
        before = ticks
        await asyncio.wait_for(Assistant._process(harness, text=text), timeout=30)
        during = ticks - before
    finally:
        stop.set()
        await tick_task
        for name, original in saved.items():
            setattr(desktop, name, original)
    return harness, during


async def main():
    print("-- the compositor is asked off the voice loop --")

    windows = [{"app_id": "kitty", "title": "~"}]
    harness, during = await run_turn(
        "what windows are open", 0.6, {"windows": hung_desktop(0.6, windows)})
    check("the loop keeps ticking while the window list is fetched",
          during >= 10, f"{during} ticks in 0.6s (was 0 before)")
    check("and the compositor was asked on another thread",
          CALLER and CALLER[0] != threading.current_thread().name, f"asked on {CALLER}")
    check("and the answer is still spoken",
          any("Kitty" in line or "window" in line for line in harness.spoken),
          f"said {harness.spoken}")

    focused = {"ok": True, "window": {"app_id": "kitty", "title": "~"}}
    harness, during = await run_turn(
        "focus the terminal", 0.6, {"focus": hung_desktop(0.6, focused)})
    check("the loop keeps ticking while a window is brought forward",
          during >= 10, f"{during} ticks in 0.6s (was 0 before)")
    check("and that was asked on another thread",
          CALLER and CALLER[0] != threading.current_thread().name, f"asked on {CALLER}")

    closed = {"ok": True, "window": {"app_id": "kitty", "title": "~"}}
    harness, during = await run_turn(
        "close this window", 0.6, {"close_focused": hung_desktop(0.6, closed)})
    check("the loop keeps ticking while a window is closed",
          during >= 10, f"{during} ticks in 0.6s (was 0 before)")
    check("and that answer is spoken too",
          any("Kitty" in line for line in harness.spoken), f"said {harness.spoken}")

    print()
    if failures:
        print(f"{failures} failed")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
