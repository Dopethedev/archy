"""The guard that catches a check writing to the state the assistant runs from.

It is the one thing standing between the suite and the owner's own state: the
run_all.sh loop compares a fingerprint of `.state`, `.sessions`, `memory.json`
and `config.local.json` before and after, and this is the rule it compares them
by. It has one exception, and the exception is the point: the suite is run on the
machine the assistant is running on, and the assistant appends to its own
conversation while the suite runs. Everything else a live file can do is a write
from a check, and has to be named.

Reads only. Nothing here writes to the live state, which is the rule it enforces.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_state  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:58s} {detail}")


def line(name, digest, mtime, size):
    """A fingerprint line, spelled the way the guard spells it.

    Before the rule, a line was name, digest and mtime and there was no exception
    for a conversation: this spells the old form so a run against that code reports
    what is missing instead of dying at the call.
    """
    build = getattr(live_state, "_line", None)
    return build(name, (digest, mtime, size)) if build else f"{name} {digest} {mtime}"


rule = getattr(live_state, "is_the_owners_conversation", None)
check("a session file that only grew is the owner's conversation", callable(rule),
      "there is no rule, so a running assistant fails the suite")


print("-- a conversation that only grew is the assistant talking --")
conversation = line(".sessions/2026-01-01T00-00-00_abc.jsonl", "d1", 1, 4000)
if callable(rule):

    check("a session file that grew is left alone",
      live_state.is_the_owners_conversation(
          ".sessions/2026-01-01T00-00-00_abc.jsonl", conversation,
          line(".sessions/2026-01-01T00-00-00_abc.jsonl", "d2", 2, 9000)),
      "a turn appended to it")
    check("the same size is not growth",
      not live_state.is_the_owners_conversation(
          ".sessions/2026-01-01T00-00-00_abc.jsonl", conversation,
          line(".sessions/2026-01-01T00-00-00_abc.jsonl", "d2", 2, 4000)),
      "rewritten with the same length, which is a write")
    check("a session file that shrank is a write, not a conversation",
      not live_state.is_the_owners_conversation(
          ".sessions/2026-01-01T00-00-00_abc.jsonl", conversation,
          line(".sessions/2026-01-01T00-00-00_abc.jsonl", "d3", 3, 12)),
      "nothing takes words back out of a conversation")
    check("a session file that appeared is a write, not a conversation",
      not live_state.is_the_owners_conversation(
          ".sessions/2026-01-02T00-00-00_def.jsonl", None,
          line(".sessions/2026-01-02T00-00-00_def.jsonl", "d4", 4, 20)),
      "a check that starts a real pi lands here")
    check("a session file that vanished is named too",
      not live_state.is_the_owners_conversation(
          ".sessions/2026-01-01T00-00-00_abc.jsonl", conversation, None),
      "the file the next start reads is gone")
    check("and every other live path is never excused",
      not any(live_state.is_the_owners_conversation(name, "a", "b") for name in
              (".state/report.json", "config.local.json", "memory.json",
               ".state/mic.json", ".sessions", ".state/sway-profile")),
      "only a conversation in .sessions can grow")

print("-- and the fingerprint round trips on the real state directory --")
before = live_state.fingerprint()
check("an unchanged state is not reported as moved",
      live_state.moved_from(before) == [], live_state.moved_from(before))
lines = before.splitlines()
check("every live file is one line of name, digest, mtime and size",
      all(len(part.split(" ")) == 4 for part in lines), f"{len(lines)} live files")
check("and the paths it watches are the ones the assistant reads back",
      set(live_state.LIVE) == {live_state.ROOT / ".state", live_state.ROOT / ".sessions",
                               live_state.ROOT / "config.local.json",
                               live_state.ROOT / "memory.json"},
      ", ".join(p.name for p in live_state.LIVE))

sessions = [ln for ln in lines if ln.startswith(".sessions/")]
if sessions:
    name = sessions[0].split(" ", 1)[0]
    # the same file, as it looked before a turn was answered into it
    was_there_before = before.replace(sessions[0], line(name, "d", 1, 1))
    check("a real conversation that grew since the snapshot is left alone",
          name not in live_state.moved_from(was_there_before), name)
else:
    print("    (no session file on this machine to check the rule against)")

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
