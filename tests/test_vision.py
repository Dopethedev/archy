"""Screen vision: the intent rule, the capture pipeline, and the wiring.

The capture test runs the real grim + ffmpeg pipeline, so it needs a Wayland
session, which is what the machine running the assistant has anyway. Everything
else is stubbed.
"""
import asyncio
import base64
import io
import json
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import archy.assistant as archy_assistant
import archy.vision as vision
from archy import status as status_mod
from archy import config, privacy
from archy.assistant import Assistant
from archy.brain import PiBrain
from archy.vision import ScreenError, as_image, capture, wanted

passed = failed = skipped = 0


def check(label, ok, detail=""):
    global passed, failed
    ok = bool(ok)                  # `A and B` can return a list, and int + list stops the run
    passed += ok
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


print("-- what counts as a request to see --")
for text in ("what's on my screen", "look at this", "take a screenshot",
             "what am i looking at", "what is this error on my screen",
             "what is this game", "read this page for me"):
    check(f"wanted {text!r}", wanted(text))
for text in ("what time is it", "open brave", "close yourself",
             "what's the weather", "remember that my name is Alex",
             "how much ram is free"):
    check(f"not wanted {text!r}", not wanted(text))

print("-- the capture pipeline, for real --")
# The compositor's screencopy fails in bursts here, for every client at once:
# measured over 25 calls, 22 worked, and in a bad window none do, grim and
# wf-recorder alike. That is the machine's state and not this code's behaviour,
# so a bad window is reported as a skip rather than as a failure, which would
# train the reader to ignore the line. A working window still has to pass.
jpeg = None
last = ""
for attempt in range(5):
    try:
        jpeg = capture()
        break
    except (ScreenError, OSError) as exc:
        last = str(exc)
        time.sleep(0.3)
if jpeg is None:
    skipped += 1
    print(f"  [SKIP] grim + ffmpeg produce a jpeg  the compositor would not "
          f"copy the frame in 5 tries: {last[:60]}")
else:
    check("grim + ffmpeg produce a jpeg", jpeg[:2] == b"\xff\xd8" and len(jpeg) > 10_000,
          f"{len(jpeg)/1024:.0f} kB")
    image = as_image(jpeg)
    check("as_image roundtrip", image["mimeType"] == "image/jpeg"
          and base64.b64decode(image["data"]) == jpeg)

print("-- the second backend --")
# Windows, macOS and X11 have no grim. The fallback is one frame from Pillow, and
# it is tested for real here too, because Pillow can grab an X11 session as well
# as a Windows one.
check("it names a backend this machine has",
      vision.backend() in ("grim", "screencapture", "grab"), vision.backend())

# Which one, though, is not this machine's business: asked whether its choice
# follows what is installed, this read "grim or the Pillow fallback", and a Mac
# answers screencapture because that is the platform's own tool there. So the
# tools and the platform are both stubbed and the priority is asserted instead.
real_which = vision.shutil.which
real_platform = vision.sys.platform
try:
    vision.shutil.which = lambda name: f"/usr/bin/{name}"
    vision.sys.platform = "darwin"
    check("grim outranks the platform's own tool", vision.backend() == "grim",
          vision.backend())
    vision.shutil.which = lambda name: "/usr/bin/screencapture" if name == "screencapture" else None
    check("a Mac without grim uses screencapture", vision.backend() == "screencapture",
          vision.backend())
    vision.sys.platform = "linux"
    check("no grim and not a Mac uses the Pillow frame", vision.backend() == "grab",
          vision.backend())
finally:
    vision.shutil.which = real_which
    vision.sys.platform = real_platform

vision.shutil.which = lambda name: None
try:
    check("with grim hidden it moves to the other backend", vision.backend() == "grab")
    try:
        jpeg = capture(attempts=2)
        check("the other backend captures a jpeg", jpeg[:2] == b"\xff\xd8" and len(jpeg) > 5_000,
              f"{len(jpeg)/1024:.0f} kB")
    except ScreenError as exc:
        # a headless session has no frame to grab; that is the machine, not this
        skipped += 1
        print(f"  [SKIP] the other backend captures a jpeg  {str(exc)[:60]}")
finally:
    vision.shutil.which = real_which

# and when neither is there, it says what it would need instead of failing oddly
real_backend = vision.backend
vision.backend = lambda: ""
try:
    capture()
    check("nothing available raises, not crash", False)
except ScreenError as exc:
    check("nothing available raises, not crash", "grim" in str(exc) and "Pillow" in str(exc),
          str(exc)[:70])
finally:
    vision.backend = real_backend

print("-- the prompt payload the brain sends --")
check("text only has no images key",
      "images" not in PiBrain._prompt_payload("hi"))
image = {"type": "image", "data": "AAA", "mimeType": "image/jpeg"}
payload = PiBrain._prompt_payload("look", [image])
check("with images the payload carries them",
      payload["images"] == [image] and payload["message"] == "look")

print("-- the turn attaches the screen when asked --")


class ScreenHarness:
    """Enough of an Assistant for _process to run end to end with stubs."""

    def __init__(self, capture_result, stream: tuple = ()):
        self._privacy_confirm = privacy.Confirmation()
        self.status = status_mod.Feed()
        self.captures = 0
        self.capture_result = capture_result
        self.log_lines = []
        self.brain = FakeBrain(stream)
        self.spoken: list[str] = []
        harness = self

        # staticmethod, so these take what the assistant passes and nothing more:
        # an instance-bound one takes an extra self, which is silently one argument
        # short and raised inside the thread rather than at the call
        def say_stream(deltas, on_spoken, _cancel):
            spoken = consume(deltas)
            harness.spoken.extend(spoken)
            if on_spoken and spoken:
                on_spoken()

        self.speaker = type("S", (), {
            "say_stream": staticmethod(say_stream),
            "say": staticmethod(lambda text: harness.spoken.append(text))})()
        self._theme_intent = Assistant._theme_intent
        self.player = type("P", (), {"wait_drained": lambda s, t=None: None})()
        self.mic_enabled = True
        self.state = "thinking"
        self.hud = None

    def log(self, who, text):
        self.log_lines.append(text)
    def _publish_heard(self, text, partial=False):
        pass
    def set_state(self, s):
        self.state = s
    def _back_to_idle(self):
        self.set_state("idle")

    def fake_capture(self):
        self.captures += 1
        result = self.capture_result
        if isinstance(result, Exception):
            raise result
        return result


class FakeBrain:
    """Answers either streaming or in one piece, so both paths are covered.

    A relay streams: pi hands the assistant the reply a token at a time. A model
    that does not stream is the case that had nothing spoken at all, so the same
    fake does both.
    """

    def __init__(self, stream: tuple = ()):
        self.seen = []
        self.stream = stream
    def model_name(self):
        return "some-relay/large-model"      # a remote model, so the switches apply
    async def ask(self, text, images=None):
        self.seen.append((text, images))
        for piece in self.stream:
            yield "delta", piece
        yield "done", "".join(self.stream) or "ok"


def consume(deltas):
    """Drain what the speaker is handed, and keep it.

    A queue was what this took when the streaming voice was written, and the
    assistant hands say_stream an iterator now, so every one of these turns ended
    in an AttributeError inside a thread: the traceback went to the console, the
    turn was never asserted on, and the checks below passed because nothing had
    been fed to anything. What it reads is returned now, so the turn can say
    whether the words it was supposed to speak arrived.
    """
    said = []
    for delta in deltas:
        if delta is None:
            continue
        said.append(delta)
    return said


async def run_turn(text, capture_result, stream: tuple = ()):
    h = ScreenHarness(capture_result, stream)
    # patch where the assistant looks the name up, not on the class
    real = archy_assistant.capture
    archy_assistant.capture = h.fake_capture
    try:
        await Assistant._process(h, text=text)
    finally:
        archy_assistant.capture = real
    return h


async def voice_checks():
    """The stub is only worth having if the turn really reaches it.

    A turn that never hands the speaker anything is a turn that says nothing out
    loud, and nothing above can see that from the outside: the previous version of
    this stub was handed a queue where an iterator arrives, so it raised inside a
    thread and every check here passed while nothing was spoken at all.
    """
    print("-- and the turn reaches the voice --")
    h = await run_turn("what's on my screen right now", b"\xff\xd8fakejpeg")
    check("an answered turn is fed to the speaker", h.spoken == ["ok"], str(h.spoken))
    h = await run_turn("what time is it", b"\xff\xd8fakejpeg")
    check("so is an ordinary one", bool(h.spoken), str(h.spoken))
    h = await run_turn("hello", b"", stream=("One. ", "Two. "))
    check("a streamed reply is spoken once, piece by piece",
          h.spoken == ["One. ", "Two. "], str(h.spoken))


async def main():
    h = await run_turn("what's on my screen right now", b"\xff\xd8fakejpeg")
    check("a screen question captures once", h.captures == 1)
    check("the capture is logged", any("looking at your screen" in s for s in h.log_lines))
    check("the image reaches the brain",
          h.brain.seen and h.brain.seen[0][1]
          and h.brain.seen[0][1][0]["data"] == base64.b64encode(b"\xff\xd8fakejpeg").decode())

    h = await run_turn("what time is it", b"\xff\xd8fakejpeg")
    check("an ordinary question captures nothing", h.captures == 0)
    check("an ordinary question sends no images", h.brain.seen and h.brain.seen[0][1] is None)

    h = await run_turn("look at this", ScreenError("grim failed"))
    check("a failed capture still asks the model",
          h.brain.seen and h.brain.seen[0][1] is None and h.captures == 1)
    check("a failed capture is reported", any("could not capture" in s for s in h.log_lines))


    await voice_checks()

    print()
    print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
