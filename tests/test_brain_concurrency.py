"""Two turns must not read the same process at once.

This is what an interrupt does: a turn is in flight, the user speaks again, the
old turn is aborted and a new one starts immediately. With one event queue shared
between them the new turn reads the old turn's end marker, decides the run it
never saw has finished, and answers with nothing, while the old turn waits out
its silence deadline.

The brain is driven against a fake pi process that answers slowly, so the race is
reproduced rather than hoped about. No relay is involved.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["JARVIS_SESSION_DIR"] = tempfile.mkdtemp(prefix="jarvis-concurrency-")
from jarvis import config  # noqa: E402
from jarvis.brain import PiBrain  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:58s} {detail}")


# a pi that takes its time and answers with whatever it was asked
FAKE = Path(tempfile.mkdtemp()) / "slow_pi.py"
FAKE.write_text('''#!/usr/bin/env python3
import json, sys, time
current = None
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except Exception:
        continue
    if msg.get("type") == "prompt":
        rid = msg.get("id")
        current = msg.get("message", "")
        print(json.dumps({"type": "response", "command": "prompt", "id": rid,
                          "success": True}), flush=True)
        print(json.dumps({"type": "agent_start"}), flush=True)
        print(json.dumps({"type": "turn_start"}), flush=True)
        for word in current.split()[:3]:
            time.sleep(0.4)
            print(json.dumps({"type": "message_update",
                              "assistantMessageEvent": {"type": "text_delta",
                                                        "delta": word + " "}}), flush=True)
        print(json.dumps({"type": "turn_end", "message": {"stopReason": "stop"}}), flush=True)
        time.sleep(0.2)
        print(json.dumps({"type": "agent_end", "messages": []}), flush=True)
    elif msg.get("type") == "abort":
        # pi stops generating; the run it was in ends
        print(json.dumps({"type": "agent_end", "messages": []}), flush=True)
''')
os.chmod(FAKE, 0o755)
config.PI_BIN = str(FAKE)


async def turn(brain, message):
    """Collect a turn's text, the way the assistant does."""
    out = ""
    async for kind, payload in brain.ask(message):
        if kind == "delta":
            out += payload
    return out.strip()


async def main():
    print("-- one turn at a time --")
    brain = PiBrain()
    await brain.start()
    a = await turn(brain, "alpha beta gamma")
    check("a plain turn answers", a == "alpha beta gamma", repr(a))
    b = await turn(brain, "one two three")
    check("and the next turn answers too", b == "one two three", repr(b))

    print("-- a turn interrupted mid flight does not poison the next --")
    brain2 = PiBrain()
    await brain2.start()
    interrupted = asyncio.create_task(turn(brain2, "first words here"))
    await asyncio.sleep(0.5)                    # it is mid-flight now
    brain2.abort()
    await asyncio.sleep(0.3)                    # give the old turn a chance to end
    second = await asyncio.wait_for(turn(brain2, "second words here"), timeout=15)
    check("the interrupting turn gets its own answer",
          second == "second words here", repr(second))
    stopped = await asyncio.wait_for(interrupted, timeout=15)
    check("the interrupted turn ended instead of hanging",
          isinstance(stopped, str), repr(stopped))

    print("-- and a burst of turns still answers in order --")
    brain3 = PiBrain()
    await brain3.start()
    results = [await turn(brain3, f"word{i} extra tail") for i in range(3)]
    check("three turns, three answers", results == ["word0 extra tail",
                                                    "word1 extra tail",
                                                    "word2 extra tail"],
          str(results))

    for b in (brain, brain2, brain3):
        b.stop()

    print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}  ({passed} checks)")
    sys.exit(1 if failed else 0)


asyncio.run(asyncio.wait_for(main(), timeout=120))
