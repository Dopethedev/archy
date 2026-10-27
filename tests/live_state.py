"""Whether a check changed the machine it was checking.

`.state` holds the report the briefing reads out as the last sweep, the
microphone switch, the policy a running gate reads, and the reminders that are
not due yet. `.sessions` is the conversation the next start continues from,
`memory.json` is what the assistant remembers, and `config.local.json` is the
owner's own settings. A check that writes to any of them is not a check.

Running the suite used to rewrite several of them: the last-sweep report came
back with a sweep nobody ran, the microphone switch and the owner's settings file
were written with the values the checks happened to end on, and the profile lynis
reads was rewritten on the way past. Nothing failed, because nothing was looking.

So `run_all.sh` fingerprints these before the suite and compares them after,
which covers every group in its list and any group added later, and fails with
the names of the files that moved. A rewrite with identical bytes counts: that
file is the one the next start reads, and the write also changes who wrote it
last.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# what the assistant writes for itself and reads back on the next start
LIVE = (ROOT / ".state", ROOT / ".sessions", ROOT / "config.local.json",
        ROOT / "memory.json")


def snapshot() -> dict:
    """Every live file, as name -> (digest, mtime). A missing path is not an error."""
    out = {}
    for entry in LIVE:
        if entry.is_file():
            files = [entry]
        elif entry.is_dir():
            files = [path for path in sorted(entry.rglob("*")) if path.is_file()]
        else:
            files = []
        for path in files:
            out[str(path.relative_to(ROOT))] = (
                hashlib.sha256(path.read_bytes()).hexdigest()[:16],
                path.stat().st_mtime_ns)
    return out


def fingerprint() -> str:
    """One line per live file, so two runs can be compared as text."""
    return "\n".join(f"{name} {digest} {mtime}"
                     for name, (digest, mtime) in sorted(snapshot().items()))


def moved_from(before: str) -> list:
    """The live files that differ from that fingerprint, in the order given."""
    was = {line.split(" ", 1)[0]: line for line in before.splitlines() if line}
    now = {name: f"{name} {digest} {mtime}"
           for name, (digest, mtime) in snapshot().items()}
    return sorted(name for name in set(was) | set(now) if was.get(name) != now.get(name))


if __name__ == "__main__":
    if sys.argv[1:] == ["snapshot"]:
        print(fingerprint())
    elif len(sys.argv) == 3 and sys.argv[1] == "diff":
        moved = moved_from(Path(sys.argv[2]).read_text())
        print(", ".join(moved))
        sys.exit(1 if moved else 0)
    else:
        sys.exit("usage: live_state.py snapshot | diff <fingerprint file>")
