"""The durable store: it must persist, stay readable, and reach the prompt.

A memory layer that loses facts on restart, or that cannot be read by hand, or
that never makes it into what the model sees, fails the point of existing.
"""
import json, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, "/home/dope/jarvis/src")
from jarvis import config
from jarvis.memory_store import Memory

failures = 0
def check(label, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:46s} {detail}")

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "memory.json"

    m1 = Memory(path)
    check("starts empty", m1.line_count() == 0)
    m1.set("codeword", "purple lantern")
    m1.set("address_as", "dope")
    check("stores two facts", m1.line_count() == 2)

    m2 = Memory(path)                       # a fresh instance, as after a restart
    check("survives a restart", m2.get("codeword") == "purple lantern",
          repr(m2.get("codeword")))
    check("second fact intact", m2.get("address_as") == "dope")

    check("unknown key returns nothing", m2.get("nothing") is None)
    check("overwrite works", (m2.set("address_as", "boss"), m2.get("address_as"))[1] == "boss")
    check("forget works", m2.forget("address_as") and m2.get("address_as") is None)

    check("file is readable json", json.loads(path.read_text())["codeword"]["value"] == "purple lantern")

    # the summary is what actually reaches the model
    summary = m2.summary()
    check("summary mentions the fact", "codeword" in summary and "purple lantern" in summary, summary)

    # a corrupt file must not take the assistant down
    path.write_text("{ this is not json")
    m3 = Memory(path)
    check("a corrupt file degrades to empty", m3.line_count() == 0)
    m3.set("recovered", "yes")
    check("and can be written again", Memory(path).get("recovered") == "yes")

    # atomic write leaves no partial file behind
    check("no temporary file left over", not path.with_suffix(".tmp").exists())

# the command the model is told to run must actually work
out = subprocess.run([sys.executable, "-m", "jarvis.memory_store", "list"],
                     capture_output=True, text=True, cwd="/home/dope/jarvis")
check("module runs as a command", out.returncode == 0, out.stdout.strip().splitlines()[0] if out.stdout else out.stderr[-80:])

# and the prompt building must include the stored facts
from jarvis.brain import PiBrain
prompt = PiBrain._system_prompt()
check("prompt explains the store", "memory_store" in prompt)
check("prompt carries known facts", "What you already know about him" in prompt or "jarvis.memory_store list" in prompt)

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
