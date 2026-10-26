"""The wake phrase, understood rather than matched.

No pretrained model exists for an arbitrary name, so the phrase is found in what
was said. That makes these cases the whole feature: the phrase with a request
attached, the phrase alone, the recogniser's imperfect spellings of a name, and
everything that must NOT wake it.
"""
from pathlib import Path
import asyncio, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from archy import config
from archy.assistant import Assistant, blip
from archy.wake import SpeechWake

failures = 0
def check(label, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:52s} {detail}")

# this test is about the shapes a call takes, so it brings its own
# phrase and aliases rather than inheriting the owner's
PHRASE = "hey archy"
ALIASES = ("archy", "archie", "arky")
w = SpeechWake(PHRASE, ALIASES)

print("-- what should wake it --")
for text, want in [
    ("Hey Archy, open Sober", "open Sober"),
    ("hey archy", ""),
    ("Hey, Archy. what is my cpu usage", "what is my cpu usage"),
    ("Archy what is the time", "what is the time"),
    ("Hey Archie open brave", "open brave"),
    ("Hey Archy take a screenshot", "take a screenshot"),
]:
    heard, rest = w.match(text)
    check(f"{text!r}", heard and rest == want, f"-> {rest!r}")

print("-- recogniser spellings of the name --")
for text in ("Hey Archy", "Hey Archie", "Hey Arky"):
    heard, _ = w.match(text)
    check(f"accepted {text!r}", heard)

print("-- what must stay quiet --")
for text in ("Hey Computer open sober", "how are you today", "open brave",
             "what is the weather", "", "   ", "the jar is empty",
             "Java is a slow language"):
    heard, _ = w.match(text)
    check(f"ignored {text!r}", not heard)

print("-- the name only counts when it comes first --")
# a video on the desk saying the name mid-sentence must not call the assistant
for text in ("Good idea, Archy, Archy, Archy.",
             "and then Archy asked a question",
             "I'm going to ask Archy to fix it",
             "tell Archy I said hello"):
    heard, _ = w.match(text)
    check(f"ignored {text!r}", not heard)
# one stray word of filler in front is still a call
for text, want in [("uh hey Archy open sober", "open sober"),
                   ("so hey Archy, what time is it", "what time is it")]:
    heard, rest = w.match(text)
    check(f"{text!r}", heard and rest == want, f"-> {rest!r}")

print("-- tidying what follows the phrase --")
for text, want in [("hey archy, so please open sober", "open sober"),
                   ("hey archy, open brave", "open brave"),
                   ("hey archy , what time is it", "what time is it")]:
    heard, rest = w.match(text)
    tidied = w.strip_greeting(rest)
    check(f"{text!r}", heard and tidied == want, f"-> {tidied!r}")


# ---------------------------------------------------------------- behaviour
class Harness(Assistant):
    """_check_wake with the microphone and voice replaced."""
    def __init__(self, transcript):
        self.hud = None                     # the real assistant always has one
        self.speech_wake = SpeechWake(PHRASE, ALIASES)
        self.transcriber = type("T", (), {"transcribe": staticmethod(lambda a: transcript)})()
        self.state = "booting"
        self.trace = []
        self.processed = []
        self.mic = type("M", (), {"drain": lambda s: None, "set_focus": lambda s, on: None})()
        wrote = []
        self.player = type("P", (), {"write": staticmethod(lambda a: wrote.append(len(a)))})()
        self.wrote = wrote
    def log(self, who, text):
        self.trace.append(f"{who}: {text}")
    def set_state(self, s):
        self.state = s
        self.trace.append(f"state={s}")
    def _back_to_idle(self):
        self.state = "idle"
        self.trace.append("state=idle")
    async def _process(self, audio=None, text=None):
        self.processed.append(text)


async def behaviour():
    print("-- behaviour after the utterance ends --")
    import numpy as np
    audio = np.zeros(16000, dtype=np.float32)

    h = Harness("Hey Archy, open Sober")
    await h._check_wake(audio, None)
    check("phrase plus a request -> handled at once",
          h.processed == ["open Sober"], f"processed={h.processed}")

    h = Harness("hey archy")
    await h._check_wake(audio, None)
    check("phrase alone -> armed and listening",
          h.state == "listening" and not h.processed, f"state={h.state}")

    h = Harness("how are you today")
    await h._check_wake(audio, None)
    check("ordinary speech -> back to idle",
          h.state == "idle" and not h.processed, f"state={h.state}")

    h = Harness("hey archy")
    await h._check_wake(audio, "hey archy")         # ready transcript path
    check("a ready transcript is used, not re-transcribed",
          h.state == "listening", f"state={h.state}")

    h = Harness("Hey Archy, open Brave")
    await h._check_wake(audio, None)
    check("it acknowledges before handling", len(h.wrote) == 1 and h.wrote[0] > 0, f"blips={h.wrote}")

    # the request must survive intact: the phrase is a prefix, not a rewrite
    for spoken, want in [
        ("Hey Archy, open Sober", "open Sober"),
        ("hey archy open github.com/Foo", "open github.com/Foo"),
        ("Hey Archy, what's the time?", "what's the time?"),
        ("Archie, open Sober", "open Sober"),       # recogniser spelling, not the phrase
        ("archy whats the weather in Berlin", "whats the weather in Berlin"),
    ]:
        h = Harness(spoken)
        await h._check_wake(audio, None)
        check(f"{spoken!r} -> {want!r}", h.processed == [want], f"processed={h.processed}")

    # and it must not wake on ordinary words that merely resemble the name
    for innocent in ["I put the jars away", "Java is a slow language", "the jar lid is stuck"]:
        h = Harness(innocent)
        await h._check_wake(audio, None)
        check(f"{innocent!r} does not wake it",
              h.state == "idle" and not h.processed, f"state={h.state} processed={h.processed}")

asyncio.run(behaviour())
print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
