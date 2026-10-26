"""The conversation on disk does not grow forever.

pi compacts the context by itself, so the model is fine; the transcript is what
keeps growing, and every start pays for it in load time. Past the budget it is
moved aside, with two things that must hold: the memory store is untouched, and
nothing is left in the session directory for pi to resume, because pi takes the
newest file it finds. Leaving one behind would silently resume an old
conversation.

Runs against a directory of its own, never the live session directory.
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from archy import config  # noqa: E402
from archy.brain import PiBrain  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:56s} {detail}")


work = Path(tempfile.mkdtemp(prefix="archy-session-test-"))
sessions = work / "sessions"
state = work / "state"
sessions.mkdir()
state.mkdir()
config.SESSION_DIR = sessions
config.STATE_DIR = state

print("-- a small conversation is left alone --")
(sessions / "2026-01-01T00-00-00-000Z_a.jsonl").write_text("x" * 1000)
check("under the budget, nothing moves", PiBrain.archive_sessions(budget_mb=1.0) == 0)
check("the file is still there", len(list(sessions.glob("*.jsonl"))) == 1)
check("size is measured across the directory", PiBrain.session_bytes() == 1000)

print("-- past the budget it moves aside --")
(sessions / "2026-01-02T00-00-00-000Z_b.jsonl").write_text("y" * 2000)
moved = PiBrain.archive_sessions(budget_mb=0.001)      # 1 KB, so this triggers
check("both files are archived", moved == 2, f"moved {moved}")
check("the session directory is empty afterwards",
      list(sessions.glob("*.jsonl")) == [], str(list(sessions.iterdir())))
archived = list((state / "session-archive").rglob("*.jsonl"))
check("the transcript is kept, just elsewhere", len(archived) == 2, str(len(archived)))
check("the archive is outside the session directory",
      not str(archived[0]).startswith(str(sessions)))

print("-- the durable facts are not part of this --")
facts = work / "memory.json"
facts.write_text('{"codeword": {"value": "purple lantern", "updated": 1}}')
config.MEMORY_FILE = facts
PiBrain.archive_sessions(budget_mb=0.0)
check("the memory store is untouched", facts.exists() and "purple lantern" in facts.read_text())

print("-- an empty directory, and a missing one, are not errors --")
check("an empty session directory is fine", PiBrain.archive_sessions() == 0)
config.SESSION_DIR = work / "does-not-exist"
check("a missing session directory is fine", PiBrain.archive_sessions() == 0)

shutil.rmtree(work, ignore_errors=True)
print(f"\n{'ALL PASS' if not failures else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
