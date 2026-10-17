"""The microphone switch remembers what it was set to.

Turning the microphone off is a deliberate act, usually because the room is not
private or the assistant keeps answering the television. Having it come back on
by itself at the next restart undoes that decision silently, and the first sign
of it is the assistant answering something he did not want it to hear.

The state is one boolean in the state directory, written atomically, read at
start-up. It never exists before the first switch, so a fresh install starts
listening.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis import config  # noqa: E402
from jarvis import mic_state  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:56s} {detail}")


work = Path(tempfile.mkdtemp(prefix="jarvis-mic-state-"))
config.STATE_DIR = work

print("-- nothing stored yet means listening --")
check("a fresh install listens", mic_state.load() is True)
check("no file is created just by reading", not (work / "microphone.json").exists())

print("-- a switch is remembered --")
mic_state.save(False)
check("it is off on disk", mic_state.load() is False)
check("the file is the only thing written",
      sorted(p.name for p in work.iterdir()) == ["microphone.json"],
      str(sorted(p.name for p in work.iterdir())))
check("it reads as plain json a person can edit",
      json.loads((work / "microphone.json").read_text())["enabled"] is False)

mic_state.save(True)
check("and back to on", mic_state.load() is True)

print("-- a broken or missing file falls back to listening --")
(work / "microphone.json").write_text("{ this is not json")
check("unreadable state listens rather than muting in silence", mic_state.load() is True)
(work / "microphone.json").write_text('{"enabled": "off"}')
check("a value that is not a boolean listens", mic_state.load() is True)
(work / "microphone.json").unlink()
check("a missing file listens", mic_state.load() is True)

print("-- writing does not leave staging files behind --")
for value in (True, False, True):
    mic_state.save(value)
leftovers = [p.name for p in work.iterdir() if p.name != "microphone.json"]
check("no temporary files remain", not leftovers, str(leftovers))

print("-- the dashboard toggle is what records it --")
# the assistant's set_mic_enabled writes it, so a restart lands where he left it
source = (Path(__file__).resolve().parents[1] / "src/jarvis/assistant.py").read_text()
check("the switch writes the state", "mic_state.save(" in source)
check("and start-up reads it", "mic_state.load()" in source)

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}  ({passed} checks)")
sys.exit(1 if failed else 0)
