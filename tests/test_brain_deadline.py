"""A brain that stops answering must be given up on, and one that keeps
talking must not be cut off.

Without a deadline a hung model process leaves the assistant stuck in
"thinking" forever, and the only way out is a restart. With too blunt a
deadline a long tool run would be killed mid-sentence, so the deadline is on
silence rather than on the total time.
"""
from pathlib import Path
import asyncio, json, os, queue, sys, threading, time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import os, tempfile as _tf
os.environ["ARCHY_SESSION_DIR"] = _tf.mkdtemp(prefix="archy-test-session-")
os.environ.setdefault("ARCHY_MEMORY_FILE", "/tmp/archy-test-memory.json")

from archy import config
import archy.brain as brain_mod
from archy.brain import BrainError, PiBrain

# the writer thread and its bound arrived with the fix; read them the tolerant way
# so a run against the code before it reports what is missing instead of dying at
# the import line
WRITE_QUEUE = getattr(brain_mod, "WRITE_QUEUE", 32)

# Starting a brain writes the policy a running gate reads, and that file is the
# owner's if nothing says otherwise. This check starts one.
config.PERMISSION_POLICY = Path(_tf.mkdtemp(prefix="archy-deadline-")) / "policy.json"

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

# the fake models the events the real pi emits: a prompt is accepted with a
# response carrying its id, and the run that follows is bracketed by agent_start
# and agent_end. A turn is only recognised inside those brackets, so a fake
# without them would be testing a protocol nothing speaks.
CHATTY = fake_pi("chatty", """
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    ev = json.loads(line)
    if ev.get("type") != "prompt":
        continue
    send({"type": "response", "command": "prompt", "id": ev.get("id"), "success": True})
    send({"type": "agent_start"})
    send({"type": "turn_start"})
    for word in ["one", "two", "three", "four", "five"]:
        time.sleep(0.25)                       # slower than the deadline below
        send({"type": "message_update",
              "assistantMessageEvent": {"type": "text_delta", "delta": word + " "}})
    send({"type": "turn_end", "message": {"stopReason": "stop"}})
    send({"type": "agent_end"})
""")

DYING = fake_pi("dying", """
for line in sys.stdin:
    sys.exit(1)                                # dies mid conversation
""")

# a pi that dies of something specific: the reason it printed for whoever started it
COMPLAINING = fake_pi("complaining", """
import sys
sys.stderr.write("error: no api key is configured for this provider\\n")
sys.stderr.flush()
sys.exit(3)
""")

# a tool that runs long: it starts a tool, goes quiet for longer than the silence
# deadline while the tool works, and then answers. A twenty second curl looks
# exactly like this, and it used to be reported as a dropped connection.
SLOW_TOOL = fake_pi("slow_tool", """
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
for line in sys.stdin:
    ev = json.loads(line)
    if ev.get("type") != "prompt":
        continue
    send({"type": "response", "command": "prompt", "id": ev.get("id"), "success": True})
    send({"type": "agent_start"})
    send({"type": "turn_start"})
    send({"type": "tool_execution_start", "toolName": "bash",
          "args": {"command": "curl -s --max-time 20 example.com"}})
    time.sleep(2.2)                            # longer than the silence deadline
    send({"type": "tool_execution_end"})
    send({"type": "message_update",
          "assistantMessageEvent": {"type": "text_delta", "delta": "it costs 60 dollars"}})
    send({"type": "turn_end", "message": {"stopReason": "stop"}})
    send({"type": "agent_end"})
""")

config.BRAIN_SILENCE_TIMEOUT_S = 1.5           # keep the test quick
config.BRAIN_TOOL_TIMEOUT_S = 6.0              # a working tool gets longer
config.BRAIN_MAX_TOOLS = 3                     # the cap under test
config.BRAIN_TURN_LOOKUP_S = 0.35              # and the time cap


RUNAWAY = fake_pi("runaway", """
import threading
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
stop = threading.Event()

def keep_looking():
    # tools run while the process still reads its input, as pi's do
    while not stop.is_set():
        send({"type": "tool_execution_start", "toolName": "bash",
              "args": {"command": "curl example.com"}})
        send({"type": "tool_execution_end"})
        time.sleep(0.05)

for line in sys.stdin:
    ev = json.loads(line)
    if ev.get("type") == "steer":
        # the steer is what ends the search and produces an answer; a model that
        # never gets one keeps looking until the test gives up on it
        stop.set()
        send({"type": "message_update",
              "assistantMessageEvent": {"type": "text_delta", "delta": "it spiked"}})
        send({"type": "turn_end", "message": {"stopReason": "stop"}})
        send({"type": "agent_end"})
        continue
    if ev.get("type") != "prompt":
        continue
    stop.clear()
    send({"type": "response", "command": "prompt", "id": ev.get("id"), "success": True})
    send({"type": "agent_start"})
    threading.Thread(target=keep_looking, daemon=True).start()""")


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

    # pi's error stream was read all along and never looked at, so a process that
    # died of a missing key said only that it had ended
    outcome, detail, _ = await drive(COMPLAINING, timeout=8)
    check("and it says why, from what pi printed before it died",
          outcome == "brainerror" and "no api key" in detail, f"{outcome}: {detail}")

    # a tool that takes longer than the silence deadline is a working process,
    # not a dead one: killing it here is what turned a slow lookup into "my
    # connection to the model dropped"
    outcome, detail, text = await drive(SLOW_TOOL, timeout=14)
    check("a tool that outruns the silence deadline is not cut off",
          outcome == "answered" and "60 dollars" in text, f"{outcome}: {detail!r}")

    # and the longer leash must not make a silent process invisible: with no
    # tool running, the old deadline still applies
    config.BRAIN_TOOL_TIMEOUT_S = 1.5
    outcome, detail, _ = await drive(SILENT, timeout=14)
    check("without a tool, silence still ends the turn",
          outcome == "brainerror" and "nothing" in detail, f"{outcome}: {detail}")
    config.BRAIN_TOOL_TIMEOUT_S = 6.0

    # a turn that keeps looking things up gets told to answer: the prompt asked
    # for this already and the model ignored it, so it is enforced here
    outcome, detail, text = await drive(RUNAWAY, timeout=14)
    check("a turn that keeps looking things up is steered to answer",
          outcome == "answered" and "spiked" in text, f"{outcome}: {detail!r}")

    # the count is not the only way a turn runs long: two slow fetches cost more
    # than eight quick ones, so time alone must be enough to trigger the steer
    config.BRAIN_MAX_TOOLS = 999
    config.BRAIN_TURN_LOOKUP_S = 0.35
    outcome, detail, text = await drive(RUNAWAY, timeout=14)
    check("a turn whose lookups are slow is steered on time alone",
          outcome == "answered" and "spiked" in text, f"{outcome}: {detail!r}")
    config.BRAIN_MAX_TOOLS = 3


async def interrupted():
    """Interrupt a turn that is parked on the queue, and time how long it takes."""
    config.PI_BIN = SILENT
    config.BRAIN_SILENCE_TIMEOUT_S = 6.0      # long, so the deadline cannot be it
    brain = PiBrain()
    await brain.start()
    got = []

    async def consume():
        async for kind, payload in brain.ask("hello"):
            got.append((kind, payload))

    turn = asyncio.ensure_future(consume())
    await asyncio.sleep(0.4)                  # it is inside the queue wait by now
    before = time.monotonic()
    brain.abort()
    try:
        await asyncio.wait_for(turn, timeout=2.0)
        ended, waited = True, time.monotonic() - before
    except asyncio.TimeoutError:
        ended, waited = False, time.monotonic() - before
        turn.cancel()
    free = not (brain._turn_lock is not None and brain._turn_lock.locked())
    brain.stop()
    return ended, waited, free, got


print("-- an interrupt ends the turn it interrupts, not the deadline --")
# The turn in flight is parked on the queue and only looked at the abort flag
# after an event arrived, so an interrupt was delivered at the same moment as the
# silence deadline: up to twenty five seconds of a turn nobody was waiting for,
# holding the one-turn lock, with the request the user had just spoken queued
# behind it and nothing on the screen to say so.
ended, waited, free, got = asyncio.run(interrupted())
check("an interrupted turn ends on the interrupt",
      ended and waited < 1.5, f"ended={ended} after {waited:.2f}s")
check("and the one-turn lock is free for the next request",
      free, "the next question would have waited behind it")
check("and it answered nothing, because it was interrupted",
      not [item for item in got if item[0] == "done"], str(got))
config.BRAIN_SILENCE_TIMEOUT_S = 1.5


print("-- a child that stops reading its input does not stop the assistant --")
# The writes went to pi's stdin from the event loop, and a write to a pipe blocks
# when the pipe is full. pi is a process this one starts and does not control: it
# can be wedged or simply not reading, and either way the loop that reads the
# microphone, drains the queues and answers the dashboard stopped with it, with no
# deadline anywhere watching.


class BlockedStdin:
    """A pipe nobody is reading: every write never comes back."""

    def __init__(self):
        self.hit = threading.Event()

    def write(self, line):
        self.hit.set()
        threading.Event().wait()          # forever, like a full pipe

    def flush(self):
        pass

    def close(self):
        pass


blocked = PiBrain()
blocked._generation = 1
blocked._write_error = ""
blocked._writes = queue.Queue(maxsize=WRITE_QUEUE)
blocked._proc = type("P", (), {"stdin": BlockedStdin(), "poll": lambda s: None})()
writer = getattr(blocked, "_write_stdin", None)


if writer is None:
    check("a write to pi is handed to a thread that can wait", False,
          "there is no writer thread, so the event loop waits on the pipe")
else:
    threading.Thread(target=writer, args=(1, blocked._proc), daemon=True).start()

    one = []

    def send_once():
        try:
            blocked._send({"type": "prompt", "message": "hello"})
            one.append("sent")
        except Exception as exc:
            one.append(type(exc).__name__)

    thread = threading.Thread(target=send_once, daemon=True)
    thread.start()
    thread.join(1.0)
    check("a send to a child that is not reading comes straight back",
          not thread.is_alive(), "the loop would have been blocked inside it")
    check("and the message did reach the writer",
          blocked._proc.stdin.hit.wait(1.0),
          f"the write has to be attempted, not dropped ({one})")

    failure = []

    def flood():
        for _ in range(WRITE_QUEUE * 3):
            try:
                blocked._send({"type": "abort"})
            except BrainError as exc:
                failure.append(str(exc))
                return
        failure.append("no error")

    flooder = threading.Thread(target=flood, daemon=True)
    flooder.start()
    flooder.join(1.0)
    check("and a child that is not reading is named, not waited for",
          failure and "not reading its input" in failure[0], str(failure)[:80])
    check("so the turn that finds it says something actionable",
          failure and "pi" in failure[0], str(failure)[:60])


asyncio.run(main())
print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
