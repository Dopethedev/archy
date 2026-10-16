"""A brain that stops answering must be given up on, and one that keeps
talking must not be cut off.

Without a deadline a hung model process leaves the assistant stuck in
"thinking" forever, and the only way out is a restart. With too blunt a
deadline a long tool run would be killed mid-sentence, so the deadline is on
silence rather than on the total time.
"""
import asyncio, json, os, sys, time

sys.path.insert(0, "/home/dope/jarvis/src")
from jarvis import config
from jarvis.brain import BrainError, PiBrain

failures = 0
def check(label, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:46s} {detail}")


def fake_pi(name: str, body: str) -> str:
    path = f"/tmp/fake_pi_{name}.py"
    with open(path, "w") as fh:
        fh.write("#!/usr/bin/env python3\nimport json, sys, time\n" + body)
    os.chmod(path, 0o755)
    return path


SILENT = fake_pi("silent", """
for line in sys.stdin:
    pass
time.sleep(3600)
""")

CHATTY = fake_pi("chatty", """
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    ev = json.loads(line)
    if ev.get("type") != "prompt":
        continue
    for word in ["one", "two", "three", "four", "five"]:
        time.sleep(0.25)                       # slower than the deadline below
        send({"type": "message_update",
              "assistantMessageEvent": {"type": "text_delta", "delta": word + " "}})
    send({"type": "agent_end"})
""")

DYING = fake_pi("dying", """
for line in sys.stdin:
    sys.exit(1)                                # dies mid conversation
""")

config.BRAIN_SILENCE_TIMEOUT_S = 1.5           # keep the test quick


async def drive(binary: str, timeout: float = 12.0):
    config.PI_BIN = binary
    brain = PiBrain()
    await brain.start()
    text = ""

    async def consume():
        nonlocal text
        async for kind, payload in brain.ask("hello"):
            if kind == "delta":
                text += payload
            elif kind == "done":
                return payload

    try:
        return "answered", await asyncio.wait_for(consume(), timeout=timeout), text
    except asyncio.TimeoutError:
        return "hung", "", text
    except BrainError as exc:
        return "brainerror", str(exc), text
    finally:
        brain.stop()


async def main():
    outcome, detail, _ = await drive(SILENT)
    check("a process that stops answering is abandoned",
          outcome == "brainerror" and "nothing" in detail,
          f"{outcome}: {detail}")

    outcome, detail, text = await drive(CHATTY)
    check("a process that keeps talking is not cut off",
          outcome == "answered" and "five" in text,
          f"{outcome}: {detail!r}")

    outcome, detail, _ = await drive(DYING, timeout=8)
    check("a process that dies is reported, not hung",
          outcome in ("brainerror", "answered"), f"{outcome}: {detail}")


asyncio.run(main())
print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
