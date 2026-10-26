"""Does it hold up on a machine that is not this one?

The assistant is a Linux program, but the parts that are not Linux-specific are
meant to run anywhere, and a claim like that rots quietly: two module-level
imports of Unix-only modules once stopped the whole thing from starting on
Windows, and reading the code did not notice. So each shape is checked here.

Nothing in this file needs a microphone, a model or a compositor.
"""
import os
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from archy import config, sweep, vision               # noqa: E402
from archy.vision import ScreenError                 # noqa: E402

passed = failed = skipped = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


print(f"-- the platform this is running on: {sys.platform} --")

# ------------------------------------------------------- Unix-only facilities
# A module that imports pwd or fcntl at the top cannot even be imported on
# Windows, and because assistant.py imports sweep, that failure took the whole
# program down before anything ran. Blocking the imports outright is the only
# way to catch it on a machine that has them.
blocked = ("pwd", "grp", "fcntl", "termios", "resource")
blocker = f'''
import builtins, sys
real_import = builtins.__import__
def fake(name, *a, **k):
    if name in {blocked!r}:
        raise ImportError("not on this platform: " + name)
    return real_import(name, *a, **k)
builtins.__import__ = fake
import archy.assistant, archy.sweep, archy.memory_store, archy.vision
import archy.hud, archy.mictest, archy.briefing, archy.__main__
print("imported")
'''
env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
out = subprocess.run([sys.executable, "-c", blocker], capture_output=True, text=True,
                     cwd=str(ROOT), env=env, timeout=180)
check("every module imports without the Unix-only ones",
      out.returncode == 0 and "imported" in out.stdout,
      ("imported" if out.returncode == 0
       else (out.stderr.strip().splitlines() or [""])[-1])[:70])

# and the answers they give without them are honest rather than empty
answer = subprocess.run(
    [sys.executable, "-c", blocker + '''
import archy.sweep as s
print(s.check_uid_zero()[0])
'''], capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=180)
check("the account check says it has nothing to read",
      "nothing to compare" in answer.stdout,
      (answer.stdout.strip().splitlines() or [""])[-1][:60])

# ------------------------------------------------------------------- the sweep
# Linux and macOS are both read, by different checks; everything else is told so
check("the sweep knows which machines it can read",
      sweep.supported() == (sys.platform.startswith("linux") or sys.platform == "darwin"),
      f"supported={sweep.supported()}")
check("and it picks the checks that match the machine",
      sweep.checks() == (sweep.MAC_CHECKS if sys.platform == "darwin" else sweep.CHECKS),
      f"{len(sweep.checks())} checks")
real_supported = sweep.supported
sweep.supported = lambda: False
try:
    line = sweep.run()
    check("a machine it cannot read is told so, in one line",
          "neither Linux nor macOS" in line and len(line.splitlines()) == 1, line[:70])
finally:
    sweep.supported = real_supported

# ------------------------------------------------------------------- the screen
# three backends now: grim where the compositor has it, screencapture on macOS,
# Pillow's grab everywhere else
which = vision.backend()
check("there is a way to look at the screen here",
      which in ("grim", "screencapture", "grab"), which)
if sys.platform == "win32":
    check("on Windows it is the portable one, since grim is Wayland only",
          which == "grab", which)
if sys.platform == "darwin":
    check("on macOS it is the platform's own screencapture",
          which == "screencapture", which)

real_which = vision.shutil.which
vision.shutil.which = lambda name: None
try:
    check("with grim gone it moves to the portable one", vision.backend() == "grab")
    try:
        jpeg = vision.capture(attempts=2)
        check("the portable capture returns a jpeg",
              jpeg[:2] == b"\xff\xd8" and len(jpeg) > 5_000, f"{len(jpeg) / 1024:.0f} kB")
    except ScreenError as exc:
        # a session with no display has no frame to grab: that is the machine
        skipped += 1
        print(f"  [SKIP] the portable capture returns a jpeg  {str(exc)[:60]}")
finally:
    vision.shutil.which = real_which

# and with nothing available at all, the error names both ways of looking
real_backend = vision.backend
vision.backend = lambda: ""
try:
    vision.capture()
    check("no way to look raises a useful error", False)
except ScreenError as exc:
    check("no way to look raises a useful error",
          "grim" in str(exc) and "Pillow" in str(exc), str(exc)[:70])
finally:
    vision.backend = real_backend

# ---------------------------------------------------------------- the launcher
# Windows has no bash, so it needs its own, and the file has to stay in the line
# endings a command prompt expects or the first line echoes as an error
cmd = ROOT / "run.cmd"
check("there is a launcher for Windows", cmd.exists())
if cmd.exists():
    raw = cmd.read_bytes()
    # a bare LF is what makes a command prompt treat the file oddly, so the count
    # that matters is LFs that are not the second half of a CRLF
    bare = raw.replace(b"\r\n", b"").count(b"\n")
    check("and it is in Windows line endings", bare == 0 and raw.count(b"\r\n") > 5,
          f"{raw.count(b'\r\n')} lines, {bare} bare")
    text = raw.decode()
    check("it starts the assistant", "-m archy" in text and "uv" in text)

print("-- a run that was asked to stop after a while --")
# `--seconds` was documented as "stop after this many seconds" and then ignored for
# every run that was not the microphone check: a flag that silently does nothing is
# worse than no flag, because the help text is where someone reads what it does.
import asyncio  # noqa: E402

import archy.__main__ as entry  # noqa: E402


class FakeAssistant:
    def __init__(self):
        self.started = False
        self.finished = False
        self.cancelled = False

    async def run(self):
        self.started = True
        try:
            await asyncio.sleep(30)              # a run that would go on all day
            self.finished = True
        except asyncio.CancelledError:
            self.cancelled = True
            raise


bounded = FakeAssistant()
started_at = time.monotonic()
asyncio.run(entry.run_limited(bounded, 0.3))
took = time.monotonic() - started_at
check("it stops when it was told to", took < 5, f"{took:.2f}s")
check("and it was running first", bounded.started is True)

whole = FakeAssistant()
asyncio.run(entry.run_limited(whole, None))
check("with no limit it runs to the end", whole.finished is True)

print("-- the waiting list, read once --")
from archy import permissions  # noqa: E402

saved_state = config.STATE_DIR
try:
    config.STATE_DIR = Path(tempfile.mkdtemp(prefix="archy-reading-"))
    config.PERMISSION_PENDING = config.STATE_DIR / "pending.json"
    check("a missing list is nothing waiting, not an error",
          permissions.reading() == {"waiting": 0, "pending": []},
          str(permissions.reading()))
    config.PERMISSION_PENDING.write_text("{ this is not json")
    check("an unreadable list says it cannot be read, not that it is empty",
          permissions.reading()["waiting"] is None, str(permissions.reading()))
    entries = [{"id": f"p{i}", "tool": "bash", "summary": f"action {i}",
                "asked": time.time()} for i in range(3)]
    entries.append({"id": "old", "tool": "bash", "summary": "stale",
                    "asked": time.time() - 10_000})
    config.PERMISSION_PENDING.write_text(json.dumps({"pending": entries}))
    reading = permissions.reading()
    check("the count and the list come from the same read",
          reading["waiting"] == 3 and len(reading["pending"]) == 3,
          f"{reading['waiting']} and {len(reading['pending'])}")
    check("and an old one is not counted", all(e["id"] != "old" for e in reading["pending"]))
    check("the two older views still agree with it",
          permissions.waiting_count() == reading["waiting"]
          and permissions.pending() == reading["pending"])
finally:
    config.STATE_DIR = saved_state
    config.PERMISSION_PENDING = saved_state / "permission-pending.json"

print()
print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)"
      + (f", {skipped} skipped" if skipped else ""))
sys.exit(1 if failed else 0)
