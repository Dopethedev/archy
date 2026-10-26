"""The deeper sweep: rkhunter and lynis, and what happens when they cannot run.

Both are asked for rather than run every time, because they are minutes and the
ordinary sweep is meant to be one breath. The fixtures in here are real output: the
lynis block is from `lynis audit system --quick --no-colors` on this machine (lynis
3.1.4, hardening index 64, 230 tests, 32 suggestions), and the rkhunter block is from
`sudo rkhunter --check --sk --nocolors --report-warnings-only` here, which took 40
seconds and printed 76 warnings, most of them the same one about an upgraded binary.

The case worth testing hardest is the one that cannot run at all. rkhunter ships as
0700 root on Arch, so this user cannot execute it and cannot read its log either, and
the honest answer is that the check did not run. A sweep that says "no rootkits" from
a check that never executed is worse than one that says nothing.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import jarvis.sweep as sweep  # noqa: E402
from jarvis import config  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    ok = bool(ok)
    passed += ok
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:58s} {detail}")


LYNIS = """
Lynis 3.1.4
  * Some tests will be skipped (as they require root permissions)
  * Some tests might fail silently or give different results

[+] System tools
  - Checking /tmp, /var/tmp and other directories

  Warnings (0):
    None

  Suggestions (3):
  * Consider hardening system services [BOOT-5264]
      * Article: Systemd features to secure service files: https://linux-audit.com/
      * Website: https://cisofy.com/lynis/controls/BOOT-5264/
  * Run pwck manually and correct any errors in the password file [AUTH-9228]
  * Configure password hashing rounds in /etc/login.defs [AUTH-9230]

  Hardening index : 64 [############        ]
  Tests performed : 230
"""

RKHUNTER = """
Warning: The file properties have changed:
         File: /usr/bin/egrep
         Current hash: 3d5a5b
Warning: The file properties have changed:
         File: /usr/bin/fgrep
         Current hash: 8f2b21
egrep: warning: egrep is obsolescent; using grep -E
Warning: Command '/usr/bin/kmod' has been replaced by a script
Warning: Hidden file found: /etc/.updated: ASCII text
egrep: warning: egrep is obsolescent; using grep -E
"""

print("-- what is read out of a real lynis report --")
lines = sweep._lynis_lines(LYNIS)
joined = " | ".join(lines)
check("the hardening index is reported", "64/100" in joined, joined[:90])
check("and how many tests ran", "230 tests" in joined, joined[:90])
check("with no warnings it says so rather than staying quiet", "no warnings" in joined)
check("the suggestions are counted as lynis counted them", "suggested 3 change" in joined, joined)
check("and listed by what they are, with their tags",
      "Consider hardening system services [BOOT-5264]" in joined and "Article" not in joined,
      joined)
check("a report that ran without root says so", "require root" in joined or
      "tests needing root" in joined, joined)
check("the notices about the audit itself are not findings",
      "fail silently" not in joined, joined)
check("an unreadable report is not called a clean one",
      "could not be read" in sweep._lynis_lines("some noise")[0],
      sweep._lynis_lines("some noise")[0])

print("-- and out of a real rkhunter report --")
lines = sweep._rkhunter_lines(RKHUNTER)
joined = " | ".join(lines)
check("its changed files are counted, not listed one by one",
      "2 file(s) no longer match" in joined, joined[:120])
check("with what a change usually means", "upgrade" in joined, joined[:120])
check("the other warnings are quoted", "kmod" in joined or "Hidden file" in joined, joined)
check("the egrep noise from its own subshell is not a warning about this machine",
      joined.count("obsolescent") == 0, joined[:160])
check("a report with nothing in it is called clean",
      sweep._rkhunter_lines("no warnings here") == ["ok: rkhunter found no warnings on this run"],
      str(sweep._rkhunter_lines("no warnings here")))

print("-- a tool that is there but cannot be run --")


def forced(tool: str, path: str | None, runnable: bool):
    sweep._find = lambda name: path if name == tool else None
    sweep.os.access = lambda p, mode: runnable


real_find, real_access = sweep._find, sweep.os.access
try:
    forced("rkhunter", "/usr/bin/rkhunter", False)
    lines = sweep.check_rkhunter()
    joined = " | ".join(lines)
    check("a root only tool is not called missing",
          "only root can run it" in joined, joined)
    check("it names the command the user can run themselves",
          "sudo rkhunter --check --sk" in joined, joined)
    check("and it never claims to have found no rootkits",
          not any("no warnings" in line for line in lines), joined)
    # lynis has no root only packaging, so the real case is a tool that is there and
    # cannot be started: /usr/bin/rkhunter is exactly that file on this machine
    forced("lynis", "/usr/bin/rkhunter", False)
    said = " ".join(sweep.check_lynis())
    check("a tool that exists and will not start is reported as that, not as clean",
          "could not be run" in said and "hardening index" not in said, said)

    forced("rkhunter", None, True)
    joined = " | ".join(sweep.check_rkhunter())
    check("a tool that really is not installed says how to install it",
          "not installed" in joined and "pacman -S rkhunter" in joined, joined)

    # a tool found outside PATH still counts as present: which() missed rkhunter
    # because it is not executable by this user, which is not the same as absent
    sweep._find = real_find
finally:
    sweep._find, sweep.os.access = real_find, real_access

# outside the stubs, so this is the real os.access looking at the real file
import os as _os
import shutil as _shutil
found = sweep._find("rkhunter")
if Path("/usr/bin/rkhunter").exists():
    check("a tool that exists but this user cannot run is still found, "
          "where shutil.which() said it was missing",
          found == "/usr/bin/rkhunter" and _os.path.exists(found)
          and not _os.access(found, _os.X_OK)
          and _shutil.which("rkhunter") is None,
          f"found={found} runnable={_os.access(found, _os.X_OK)} which={_shutil.which('rkhunter')}")

print("-- a tool that runs but says nothing useful --")


def fake_run(payload: str, code: int = 0):
    def runner(argv, **kwargs):
        return type("P", (), {"returncode": code, "stdout": payload, "stderr": ""})()
    return runner


real_subprocess_run = sweep.subprocess.run
real_access2 = sweep.os.access
try:
    sweep._find = lambda name: "/usr/bin/lynis"
    # the point here is what happens after a tool starts, so it is made runnable
    # rather than depending on what this machine happens to have installed
    sweep.os.access = lambda p, mode: True
    sweep.subprocess.run = fake_run(LYNIS)
    lines = sweep.check_lynis()
    check("a lynis that runs is read", any("hardening index 64" in line for line in lines), str(lines))
    sweep.subprocess.run = fake_run("", 0)
    check("one that exits silently is a warning, not a clean machine",
          "without a report" in " ".join(sweep.check_lynis()),
          " ".join(sweep.check_lynis()))
    sweep.subprocess.run = fake_run(RKHUNTER)
    lines = sweep.check_rkhunter()
    check("an rkhunter that runs is read",
          any("no longer match" in line or "warning" in line for line in lines), str(lines))

    def exploding(argv, **kwargs):
        raise OSError("no such file")

    sweep.subprocess.run = exploding
    check("a tool that cannot be started is reported, not raised",
          "could not be run" in " ".join(sweep.check_lynis()), " ".join(sweep.check_lynis()))
finally:
    sweep.subprocess.run = real_subprocess_run
    sweep.os.access = real_access2

print("-- the one test that asks for a password --")
# Measured: lynis's NETW-2706 runs `resolvectl statistics`, and on this machine polkit
# answers that with an authentication dialog on the desktop. A deep sweep that asks
# for the password is not one anybody keeps running, and lynis 3.1.7 has no flag to
# skip a test, so it is a profile the sweep writes itself.
import tempfile
real_state = config.STATE_DIR
config.STATE_DIR = Path(tempfile.mkdtemp())
try:
    profile = sweep._lynis_profile()
    body = profile.read_text() if profile else ""
    check("a profile is written where lynis will read it",
          profile is not None and profile.exists() and profile.name == "lynis.prf",
          str(profile))
    check("it leaves out the test that puts a dialog on the desktop",
          "skip-test=NETW-2706" in body, body.replace("\n", " ")[:90])
    check("and says why, for whoever reads it next",
          "authentication" in body or "password" in body, body[:90])

    captured = {}
    real_run = sweep.subprocess.run
    sweep.subprocess.run = lambda argv, **k: (captured.update(argv=argv),
                                              type("P", (), {"returncode": 0, "stdout": LYNIS,
                                                             "stderr": ""})())[1]
    try:
        sweep.check_lynis()
    finally:
        sweep.subprocess.run = real_run
    check("and it is passed to lynis as a profile",
          "--profile" in captured.get("argv", [])
          and str(profile) in captured.get("argv", []), str(captured.get("argv")))
    check("not as a flag lynis does not have",
          "--skip-test" not in captured.get("argv", []), str(captured.get("argv")))

    config.SWEEP_LYNIS_SKIP_TESTS = ()
    check("with nothing to skip, no profile is written at all",
          sweep._lynis_profile() is None)
    config.SWEEP_LYNIS_SKIP_TESTS = ("NETW-2706",)
    import tempfile as _tempfile
    blocked = Path(_tempfile.mkdtemp()) / "a-file"
    blocked.write_text("not a directory")
    config.STATE_DIR = blocked / "inside"          # a file cannot contain a directory
    check("a state directory that cannot be written is not a crash",
          sweep._lynis_profile() is None and not (blocked / "inside").exists())
finally:
    config.STATE_DIR = real_state
    config.SWEEP_LYNIS_SKIP_TESTS = ("NETW-2706",)

print("-- what a deep sweep is asked for --")
for text in ("run a deep sweep", "deep scan", "do a thorough sweep", "check for rootkits",
             "audit the system", "run lynis"):
    check(f"asked for: {text!r}", sweep.deep_wanted(text))
for text in ("run a security sweep", "am i infected", "check my computer", "what is on my screen"):
    check(f"not asked for: {text!r}", not sweep.deep_wanted(text))

print("-- and it is only in the sweep that was asked for --")


def stub_check():
    return ["ok: stub"]


real_checks, real_deep, real_supported = sweep.checks, sweep.deep_checks, sweep.supported
ran = []


def recording(name):
    def inner():
        ran.append(name)
        return [f"ok: {name}"]
    return inner


try:
    sweep.checks = lambda: (recording("ordinary"),)
    sweep.deep_checks = lambda: (recording("rkhunter"), recording("lynis"))
    sweep.supported = lambda: True            # this is about which checks run
    ran.clear()
    report = sweep.run()
    check("an ordinary sweep does not run the deep checks",
          ran == ["ordinary"] and "deep" not in report, str(ran))
    ran.clear()
    report = sweep.run(deep=True)
    check("a deep one runs them after the ordinary checks",
          ran == ["ordinary", "rkhunter", "lynis"], str(ran))
    check("and the report says it was the deeper one", "deeper checks" in report, report[:80])
finally:
    sweep.checks, sweep.deep_checks, sweep.supported = real_checks, real_deep, real_supported

real_platform = sys.platform
try:
    sys.platform = "darwin"
    check("macOS gets no rkhunter or lynis, which are not there to run",
          sweep.deep_checks() == (), str(sweep.deep_checks()))
    sys.platform = "win32"
    check("and neither does Windows", sweep.deep_checks() == ())
finally:
    sys.platform = real_platform

print("-- the timers are bounded, because these are minutes --")
check("lynis has a cap", 60 <= config.SWEEP_LYNIS_TIMEOUT_S <= 600,
      f"{config.SWEEP_LYNIS_TIMEOUT_S:.0f}s")
check("rkhunter has one too", 60 <= config.SWEEP_RKHUNTER_TIMEOUT_S <= 900,
      f"{config.SWEEP_RKHUNTER_TIMEOUT_S:.0f}s")
check("and the words that ask for a deep sweep are separate from the ordinary ones",
      not any(word in config.SWEEP_WORDS for word in config.SWEEP_DEEP_WORDS),
      str([w for w in config.SWEEP_DEEP_WORDS if w in config.SWEEP_WORDS]))

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}  ({passed} checks)")
sys.exit(1 if failed else 0)
