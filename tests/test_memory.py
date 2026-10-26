"""Does the conversation survive a restart?

Starts a brain, tells it something, stops it completely, starts a new one and
asks what it was told. That is the only way to know whether --continue is
actually resuming anything.
"""
from pathlib import Path
import asyncio, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import os, tempfile as _tf
os.environ["ARCHY_SESSION_DIR"] = _tf.mkdtemp(prefix="archy-test-session-")
os.environ.setdefault("ARCHY_MEMORY_FILE", "/tmp/archy-test-memory.json")

from archy import config
from archy.brain import BrainError, PiBrain

SECRET = "purple lantern"

async def ask(brain: PiBrain, text: str) -> str:
    answer = ""
    async for kind, payload in brain.ask(text):
        if kind == "done":
            answer = payload
    return answer

async def main():
    config.PI_CONTINUE = True

    # This is the one test that needs a live model, and the free relays are the
    # weak link: they stall, and "pi said nothing" is a fact about the relay
    # rather than about resuming a conversation. Reported as a skip so a hiccup
    # in someone else's service does not read as broken code, which would train
    # the reader to ignore the suite. A wrong answer is still a failure.
    try:
        first = PiBrain()
        await first.start()
        reply = await ask(first, f"Please remember this for later: the codeword is {SECRET}. "
                                 "Just acknowledge briefly.")
        print(f"first session said: {reply[:120]!r}")
        first.stop()
        print("first session stopped\n")

        await asyncio.sleep(1)

        second = PiBrain()
        await second.start()
        reply2 = await ask(second, "What was the codeword I asked you to remember? "
                                   "Answer with just the words.")
        second.stop()
    except BrainError as exc:
        print(f"\nSKIP: the model did not answer, so resumption was not tested: {exc}")
        return 0
    print(f"second session said: {reply2[:160]!r}")

    remembered = SECRET.split()[0] in reply2.lower() or SECRET in reply2.lower()
    print(f"\n{'PASS: memory survived the restart' if remembered else 'FAIL: the restart lost it'}")
    return 0 if remembered else 1

sys.exit(asyncio.run(main()))
