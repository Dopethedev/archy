"""Remembering is done here, not by a shell command and not by a model.

Before this, "remember that the router is in the hall" became a `python -m
jarvis.memory_store set` command for the model to run: a round trip to a relay to
write a fact into a file on this machine, and, with the permission gate on, an
approval before it could. Asking what was already known was the same trip in the
other direction.

The checks below hold all three of those still: a fact stored by saying it, what is
known read back without a model, one fact dropped by name, and, under all of it, that
the brain was never asked. The brain being asked is the thing that used to happen, so
it is the thing that has to fail if this is undone.
"""
from pathlib import Path
import asyncio
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import os
os.environ["JARVIS_SESSION_DIR"] = tempfile.mkdtemp(prefix="jarvis-test-session-")
STORE = Path(tempfile.mkdtemp(prefix="jarvis-test-memory-")) / "memory.json"
os.environ["JARVIS_MEMORY_FILE"] = str(STORE)

from jarvis import config  # noqa: E402
from jarvis import memory_store as store
from jarvis import privacy
from jarvis import status as status_mod
from jarvis.assistant import Assistant
from jarvis.memory_store import Memory

failures = 0
checks = 0
def check(label, ok, detail=""):
    global failures, checks
    checks += 1
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:54s} {detail}")


print("-- what a sentence asks to keep --")

cases = [
    ("remember that the router is in the hall", ("router", "in the hall")),
    ("remember my sister's name is Asha", ("sister's_name", "Asha")),
    ("note that the codeword is purple lantern", ("codeword", "purple lantern")),
    ("keep in mind that the spare key is under the mat", ("spare_key", "under the mat")),
    ("don't forget that the bank is the corner one", ("bank", "the corner one")),
    ("remember that I take my coffee black",
     ("i_take_my_coffee_black", "I take my coffee black")),
]
for said, expected in cases:
    got = store.remember_intent(said)
    check(f"kept: {said[:38]}", got == expected, f"{got}")

for said in ["remember my address?",
             "remember to call mum at five",          # an errand, not a fact
             "remember",
             "what do you know about me",
             "open brave"]:
    got = store.remember_intent(said)
    check(f"not a fact to keep: {said[:30]}", got is None, f"{got}")

print("-- what a sentence asks to hear --")
yes = ["what do you know about me", "what do you remember",
       "what do you know about me so far now", "list what you remember",
       "read out your memory", "tell me what you know about me"]
for said in yes:
    check(f"asking: {said[:36]}", store.recall_intent(said))
for said in ["what time is it", "remember that I take my coffee black", "what's open"]:
    check(f"not asking: {said[:32]}", not store.recall_intent(said))


print("-- the store itself, for real --")
memory = Memory(STORE)
memory.set("router_location", "the router is in the hall")
ok = store.forget_intent("forget my router location", memory)
check("a named fact is dropped by what a person would call it",
      ok == "Forgotten: router location." and Memory(STORE).facts == {}, f"{ok}")
check("and it is gone from the file, not only from this object",
      "router_location" not in STORE.read_text(), STORE.name)

memory.set("bank", "the corner one")
ok = store.forget_intent("forget the bank", memory)
check("a name that is only part of a stored one still lands",
      ok == "Forgotten: bank." and Memory(STORE).facts == {}, f"{ok}")

empty = Memory(STORE)
check("a name that matches nothing is said here, not passed to a model",
      store.forget_intent("forget the wifi password", empty)
      == "I have nothing stored about the wifi password.",
      store.forget_intent("forget the wifi password", empty))
check("and forgetting your data is the privacy question too",
      store.forget_intent("forget my data", empty) is None)
# the long way of asking for the whole store, which read as one oddly named fact
# and answered "I have nothing stored about everything you know about me"
for said in ["forget everything you know about me", "delete everything",
             "forget all of it about me", "forget everything about me"]:
    check(f"the whole store is not one fact: {said[:26]}",
          store.forget_intent(said, empty) is None, str(store.forget_intent(said, empty))[:40])
check("forgetting everything is still the privacy question",
      store.forget_intent("forget everything", empty) is None)
check("and a sentence that is not a forget at all is left alone",
      store.forget_intent("forget", empty) is None
      and store.forget_intent("what's the weather", empty) is None)

memory.set("router_location", "the router is in the hall")
memory.set("bank", "the corner one")
check("more than one match is a question rather than a guess",
      store.forget_intent("forget the", memory) is None
      or "more than one" in (store.forget_intent("forget r", memory) or ""),
      store.forget_intent("forget r", memory))
check("neither of those was dropped", len(Memory(STORE).facts) == 2)


many = Memory(Path(tempfile.mkdtemp()) / "many.json")
pure = Memory(Path(tempfile.mkdtemp()) / "pure.json")
pure.set("codeword", "purple lantern")
pure.set("i_take_my_coffee_black", "I take my coffee black")
said = store.spoken_recall(pure)
check("a note is read out as the sentence it is, not as a name and a value",
      "I take my coffee black" in said and "i take my coffee black is" not in said, said)
check("a name and a value are read as a pair", "codeword is purple lantern" in said, said)
check("nothing stored says so", store.spoken_recall(many) ==
      "I have nothing stored about you yet.")
for n in range(11):
    many.set(f"fact_{n}", f"value {n}")
said = store.spoken_recall(many)
# 11 stored, 8 read out, the rest counted rather than read all evening
listed = said.split(": ", 1)[-1]          # past "Here is what you have told me"
check("a long store is cut off with the count, not read out forever",
      said.endswith("and 3 more.") and listed.count(" is ") == 8,
      f"{listed.count(' is ')} pairs, {said[-14:]}")

print("-- a turn remembers, answers and forgets without the model --")


class Harness:
    """Enough of an Assistant for _process, with a brain that counts its turns."""

    def __init__(self):
        self._privacy_confirm = privacy.Confirmation()
        self.status = status_mod.Feed()
        self.spoken: list[str] = []
        self.brain = CountingBrain()
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


class CountingBrain:
    """A model that records being asked, because being asked is the old behaviour."""

    def __init__(self):
        self.asked: list[str] = []
    def model_name(self):
        return "some-relay/large-model"
    async def ask(self, text, images=None):
        self.asked.append(text)
        yield "done", "the model answered"


async def turn(text: str):
    harness = Harness()
    await asyncio.wait_for(Assistant._process(harness, text=text), timeout=30)
    return harness


async def main():
    said = "remember that the spare key is under the mat"
    harness = await turn(said)
    check("saying it stores it", Memory(STORE).get("spare_key") == "under the mat",
          str(Memory(STORE).facts))
    check("and says one short sentence back",
          harness.spoken == ["Noted: spare key is under the mat."], f"{harness.spoken}")
    check("and the model was never asked", harness.brain.asked == [], f"{harness.brain.asked}")

    harness = await turn("what do you know about me")
    check("asking reads it back without the model",
          harness.brain.asked == [] and any("spare key is under the mat" in line
                                            for line in harness.spoken),
          f"{harness.spoken}")

    harness = await turn("forget the spare key")
    check("forgetting drops it, still without the model",
          harness.brain.asked == [] and Memory(STORE).get("spare_key") is None,
          f"{harness.spoken} {harness.brain.asked}")
    check("and says so", harness.spoken == ["Forgotten: spare key."], f"{harness.spoken}")

    # the clear-everything request keeps its own confirmation and its own two
    # minutes, and is not answered as a single forgotten fact
    harness = await turn("forget everything you know about me")
    check("clearing the lot is still the privacy question, not a forget",
          harness.brain.asked == [] and not any("Forgotten" in line
                                                for line in harness.spoken)
          and any("confirm" in line for line in harness.spoken), f"{harness.spoken}")

    # an ordinary question is still the model's, or this would be answering everything
    harness = await turn("what's the capital of Peru")
    check("an ordinary question still goes to the model",
          harness.brain.asked == ["what's the capital of Peru"], f"{harness.brain.asked}")

    # the whole point of doing it here: the gate is not involved
    pending = config.STATE_DIR / "permissions-pending.json"
    if pending.exists():
        before = pending.read_text()
    else:
        before = ""
    await turn("remember that the wifi password is hunter2")
    after = pending.read_text() if pending.exists() else ""
    check("nothing was left waiting on an approval", before == after,
          repr(after[:60]))

    print()
    if failures:
        print(f"{failures} failed")
        return 1
    print(f"ALL PASS  ({checks} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
