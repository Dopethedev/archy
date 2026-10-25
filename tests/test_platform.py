"""Does it hold up on a machine that is not this one?

The assistant is a Linux program, but the parts that are not Linux-specific are
meant to run anywhere, and a claim like that rots quietly: two module-level
imports of Unix-only modules once stopped the whole thing from starting on
Windows, and reading the code did not notice. So each shape is checked here.

Nothing in this file needs a microphone, a model or a compositor.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jarvis import sweep, vision                      # noqa: E402
from jarvis.vision import ScreenError                 # noqa: E402

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
import jarvis.assistant, jarvis.sweep, jarvis.memory_store, jarvis.vision
import jarvis.hud, jarvis.mictest, jarvis.briefing, jarvis.__main__
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
import jarvis.sweep as s
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
which = vision.backend()
check("there is a way to look at the screen here", which in ("grim", "grab"), which)
if sys.platform == "win32":
    check("on Windows it is the portable one, since grim is Wayland only",
          which == "grab", which)

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
    check("it starts the assistant", "-m jarvis" in text and "uv" in text)

print()
print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)"
      + (f", {skipped} skipped" if skipped else ""))
sys.exit(1 if failed else 0)
