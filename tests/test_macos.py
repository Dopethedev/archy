"""Does the macOS side actually work, on a machine that is not one?

None of this runs on a Mac, and it is all still worth checking. The parsing is
what breaks in this kind of code, and parsing can be checked anywhere: the vm_stat
output below is a real capture from an Apple Silicon machine, the sysctl and lsof
shapes are the real ones, and a plist and a Mach-O header are written to a
temporary directory. The commands themselves are faked as small executables on
PATH, so the argument building, the subprocess call and the parsing all run for
real rather than being stubbed away.

What cannot be checked here is whether the mach kernel hands back the right ticks
on a real Mac, so the ctypes struct is checked for its shape instead, and the
mach call failing on a machine that has no mach is checked to degrade rather than
raise.

Nothing in this file needs a microphone, a model or a compositor.
"""
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PIL import Image                                # noqa: E402

import jarvis.__main__ as main_mod                    # noqa: E402
import jarvis.audio as audio                          # noqa: E402
import jarvis.sweep as sweep                          # noqa: E402
import jarvis.sysinfo as sysinfo                      # noqa: E402
import jarvis.vision as vision                        # noqa: E402

passed = failed = skipped = 0
TEMPS = []


def check(label, ok, detail=""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


def workdir(prefix):
    """A scratch directory, remembered so it can be cleaned up at the end."""
    path = Path(tempfile.mkdtemp(prefix=f"jarvis-{prefix}-"))
    TEMPS.append(path)
    return path


def fakebin(prefix, files):
    """A directory of stand-in commands, executable and on demand."""
    path = workdir(prefix)
    for name, body in files.items():
        tool = path / name
        tool.write_text("#!/bin/sh\n" + body)
        tool.chmod(0o755)
    return path


# A real `vm_stat` from an Apple Silicon Mac: 16 KiB pages, every counter the
# parser looks at, spelled the way the tool spells them.
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              128230.
Pages active:                            794612.
Pages inactive:                          897256.
Pages speculative:                        25662.
Pages throttled:                              0.
Pages wired down:                        136740.
Pages purgeable:                            972.
"Translation faults":                 313387922.
Pages copy-on-write:                   13439750.
Pages zero filled:                    241432617.
Pages reactivated:                       331910.
Pages purged:                            182352.
File-backed pages:                       868337.
Anonymous pages:                         849193.
Pages stored in compressor:              139895.
Pages occupied by compressor:             58043.
Decompressions:                            8996.
Compressions:                            149041.
Pageins:                                1952492.
Pageouts:                                  8141.
Swapins:                                      0.
Swapouts:                                     0.
"""
SYSCTL = ('case "$2" in\n'
          '  hw.memsize) echo "17179869184";;\n'
          '  kern.boottime) echo "{ sec = 1761300000, usec = 123456 } Thu Oct 24 09:00:00 2026";;\n'
          '  *) echo "";;\n'
          'esac\n')

print("-- the mach cpu struct, which cannot be run here --")
info = sysinfo._HostCpuLoadInfo()
check("it is four 32-bit counters", ctypes.sizeof(info) == 16, f"{ctypes.sizeof(info)} bytes")
check("idle is the third of them", sysinfo._CPU_STATE_IDLE == 2)
check("the flavour asked for is host cpu load info", sysinfo._HOST_CPU_LOAD_INFO == 3)

print("-- the cpu arithmetic, which is the same on both platforms --")
real_linux_ticks, real_mac_ticks = sysinfo._linux_cpu_ticks, sysinfo._mac_cpu_ticks
real_is_linux = sysinfo.IS_LINUX
clock = [(100, 400)]
sysinfo._linux_cpu_ticks = lambda: clock[0]
sysinfo._mac_cpu_ticks = lambda: clock[0]
# the delta arithmetic is the same on both platforms and lives in one place, so
# the branch is named rather than left to whatever this machine happens to be:
# on Windows there is no branch at all, and the numbers would come back zero
sysinfo.IS_LINUX = True
try:
    pct, state = sysinfo.cpu_percent((0, 0))
    check("a quarter of the ticks idle reads as three quarters busy",
          abs(pct - 75.0) < 0.001, f"{pct:.2f}")
    check("the state comes back for the next call", state == (100, 400))
    pct, _ = sysinfo.cpu_percent(state)
    check("the same ticks twice is no interval, so no claim", pct == 0.0)
    clock[0] = (5, 100)
    pct, state = sysinfo.cpu_percent((4294967290, 4294967396))
    check("a counter that went backwards is not a spike",
          pct == 0.0 and state == (5, 100), f"{pct:.2f}")
finally:
    sysinfo._linux_cpu_ticks, sysinfo._mac_cpu_ticks = real_linux_ticks, real_mac_ticks
    sysinfo.IS_LINUX = real_is_linux

print("-- macOS readings, through commands that really run --")
REAL_PATH = os.environ["PATH"]
REAL_PLATFORM = (sysinfo.IS_LINUX, sysinfo.IS_MAC)
REAL_MEMSIZE = sysinfo._HW_MEMSIZE


def as_mac(path: str):
    os.environ["PATH"] = f"{path}{os.pathsep}{REAL_PATH}"
    sysinfo.IS_LINUX, sysinfo.IS_MAC = False, True
    sysinfo._HW_MEMSIZE = None


def as_this_machine():
    os.environ["PATH"] = REAL_PATH
    sysinfo.IS_LINUX, sysinfo.IS_MAC = REAL_PLATFORM
    sysinfo._HW_MEMSIZE = REAL_MEMSIZE


def read_through_real_commands():
    """The mac readings, driven by stand-in commands that really run.

    Nothing is stubbed at the Python level here: the stand-ins are executables
    found on PATH, so the argument building, the subprocess call and the parsing
    are the real ones.
    """
    good = fakebin("macos-good", {
        "vm_stat": "cat <<'EOF'\n" + VM_STAT + "EOF\n",
        "sysctl": SYSCTL,
        "osx-cpu-temp": 'echo "52.3\\xc2\\xb0C"\n',
    })
    as_mac(str(good))
    try:
        pct, used_gb = sysinfo.memory()
        check("memory is read out of real vm_stat words", 0.0 <= pct <= 100.0, f"{pct:.1f}%")
        check("and stays a believable number of gigabytes", 0.0 <= used_gb <= 16.01,
              f"{used_gb:.2f} GiB")
        check("the total is remembered rather than asked again",
              sysinfo._HW_MEMSIZE == 17179869184)
        up = sysinfo.uptime_seconds()
        check("uptime comes off kern.boottime", up is not None and up > 0,
              f"{up:,.0f} s" if up else "None")
        check("the temperature is read when a tool will say it",
              sysinfo.temperature_c() == 52.3, str(sysinfo.temperature_c()))
    finally:
        as_this_machine()

    # An exact case, so the arithmetic is pinned rather than merely in range.
    # 3000 pages of 4096 bytes, of which 2000 are reclaimable: a third in use.
    exact = fakebin("macos-exact", {
        "vm_stat": ("cat <<'EOF'\nMach Virtual Memory Statistics: (page size of 4096 bytes)\n"
                    "Pages free:                               1000.\n"
                    "Pages inactive:                           1000.\n"
                    "Pages speculative:                           0.\n"
                    "Pages active:                              500.\nEOF\n"),
        "sysctl": '[ "$2" = "hw.memsize" ] && echo "12288000"\n',
    })
    as_mac(str(exact))
    try:
        pct, used_gb = sysinfo.memory()
        check("a third of the pages in use reads as 33 percent",
              abs(pct - 33.3333) < 0.01, f"{pct:.4f}")
        check("and the same figure in gigabytes",
              abs(used_gb - 4096000 / 1024**3) < 1e-9, f"{used_gb:.6f}")
        check("reclaimable pages are not counted as used", pct < 50.0)
    finally:
        as_this_machine()

    # The counters do not have to add up: a machine up for weeks can hold more
    # reclaimable pages than the total suggests. Subtracting without clamping
    # there reports negative memory, which is the bug this check exists for.
    overfull = fakebin("macos-overfull", {
        "vm_stat": ("cat <<'EOF'\nMach Virtual Memory Statistics: (page size of 4096 bytes)\n"
                    "Pages free:                               2000.\n"
                    "Pages inactive:                           1500.\n"
                    "Pages speculative:                           0.\nEOF\n"),
        "sysctl": '[ "$2" = "hw.memsize" ] && echo "4096000"\n',
    })
    as_mac(str(overfull))
    try:
        pct, used_gb = sysinfo.memory()
        check("more reclaimable memory than there is memory reads as none in use",
              pct == 0.0 and used_gb == 0.0, f"{pct:.1f}% {used_gb:.2f} GiB")
    finally:
        as_this_machine()

    broken = fakebin("macos-broken", {
        "vm_stat": "echo 'Mach Virtual Memory Statistics:'\n",
        "sysctl": 'echo "17179869184"\n',
    })
    as_mac(str(broken))
    try:
        try:
            sysinfo._mac_memory()
            check("vm_stat with no page size is refused", False)
        except ValueError as exc:
            check("vm_stat with no page size is refused", "page size" in str(exc),
                  str(exc)[:50])
        check("and the surface answers rather than raising", sysinfo.memory() == (0.0, 0.0))
        check("a boot time it cannot parse is None, not a made up figure",
              sysinfo.uptime_seconds() is None)
    finally:
        as_this_machine()


if os.name != "posix":
    skipped += 1
    print("  [SKIP] the mac readings, through commands that really run  this "
          "machine has no sh to stand one up with")
else:
    read_through_real_commands()


# A machine that claims to be a Mac and is not: there is no mach kernel to ask,
# and that has to be a reading of nothing rather than an exception in a thread
# that cannot be allowed to die. On a real Mac the call works, so there is
# nothing to see here and the check is skipped rather than asserted falsely.
if sys.platform == "darwin":
    skipped += 1
    print("  [SKIP] no mach kernel to fail on  this machine has one")
else:
    sysinfo.IS_LINUX, sysinfo.IS_MAC = False, True
    try:
        pct, state = sysinfo.cpu_percent((0, 0))
        check("no mach kernel means no cpu reading, not a crash",
              pct == 0.0 and state == (0, 0))
        check("no mach kernel means no temperature either", sysinfo.temperature_c() is None)
    finally:
        as_this_machine()

print("-- the sweep, on a Mac it cannot see --")
check("the scratch list covers the places a Mac payload lands",
      all(part in sweep.MAC_SCRATCH for part in
          ("/tmp/", "/private/tmp/", "/var/tmp/", "/users/shared/", "/downloads/")))

tree = workdir("launchagents")
agents = tree / "LaunchAgents"
agents.mkdir()
binary = tree / "helper"
binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)      # a 64-bit Mach-O header
binary.chmod(0o755)
script = tree / "nightly.sh"
script.write_text("#!/bin/sh\n")
script.chmod(0o755)


def write_plist(name, target, program_key="Program"):
    (agents / name).write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        f'  <key>Label</key><string>{name}</string>\n'
        f'  <key>{program_key}</key><string>{target}</string>\n'
        '</dict></plist>\n')


write_plist("com.example.fine.plist", str(script))
write_plist("com.example.scratch.plist", "/scratch/payload.sh")
write_plist("com.example.gone.plist", str(tree / "not-there"))
write_plist("com.example.helper.plist", str(binary))
write_plist("com.example.wide.plist", str(script))
(agents / "com.example.wide.plist").chmod(0o666)

DIRS = (("test agents", agents),)
real_scratch = sweep.MAC_SCRATCH
real_complaint = sweep._codesign_complaint
# the fixture lives under /tmp, and /tmp is one of the real scratch directories,
# so the controlled list is used here and the real one is checked as data above
sweep.MAC_SCRATCH = ("/scratch/",)
sweep._codesign_complaint = lambda path, cache: ""
try:
    found = sweep.check_mac_launch_items(dirs=DIRS)
    joined = "\n".join(found)
    check("a launch item running from a scratch directory is an alert",
          any(f.startswith("alert") and "scratch" in f for f in found), joined[:70])
    check("a launch item pointing at nothing is a warning",
          any(f.startswith("warn") and "not there" in f for f in found))
    check("a launch item anyone can rewrite is an alert",
          any(f.startswith("alert") and f"{agents}/com.example.wide.plist" in f for f in found))
    check("and the one that is fine is not named",
          not any("com.example.fine" in f for f in found))
finally:
    pass

sweep._codesign_complaint = lambda path, cache: "code object is not signed at all"
try:
    found = sweep.check_mac_launch_items(dirs=DIRS)
    check("an unsigned binary behind a launch item is a warning",
          any(f.startswith("warn") and "not signed" in f for f in found),
          "\n".join(found)[:70])
finally:
    sweep._codesign_complaint = real_complaint
    sweep.MAC_SCRATCH = real_scratch

check("a script is not sent to codesign, having no signature to check",
      sweep._is_macho(script) is False and sweep._is_macho(binary) is True)
if shutil.which("codesign") is None:
    check("with no codesign on the machine, nothing is invented about a signature",
          sweep._codesign_complaint(str(binary), {}) == "")
else:
    skipped += 1
    print("  [SKIP] no codesign on this machine  there is one here, so the "
          "missing-tool path cannot be seen")

clean = workdir("launchagents-clean")
(clean / "LaunchAgents").mkdir()
(clean / "LaunchAgents" / "com.example.quiet.plist").write_text(
    '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>\n'
    '<key>ProgramArguments</key><array><string>/bin/sh</string>'
    '<string>-c</string><string>true</string></array>\n</dict></plist>\n')
found = sweep.check_mac_launch_items(dirs=(("test agents", clean / "LaunchAgents"),))
check("a quiet machine gets one ok line, not silence",
      len(found) == 1 and found[0].startswith("ok"), found[0][:60])
check("ProgramArguments is read when there is no Program",
      "not there" not in " ".join(found))

print("-- the sweep's other mac checks --")
real_run = sweep._run
try:
    sweep._run = lambda argv, timeout=30.0: "assessments enabled\n"
    check("Gatekeeper on is reported as on", sweep.check_mac_gatekeeper()[0].startswith("ok"))
    sweep._run = lambda argv, timeout=30.0: "assessments disabled\n"
    check("Gatekeeper off is an alert", sweep.check_mac_gatekeeper()[0].startswith("alert"))
    sweep._run = lambda argv, timeout=30.0: "who knows\n"
    check("an answer nobody expected is a warning, not a verdict",
          sweep.check_mac_gatekeeper()[0].startswith("warn"))

    sweep._run = lambda argv, timeout=30.0: (
        "enabled\tactive\tteamID\tbundleID (version)\tname\t[state]\n"
        "*\t*\tTEAMID0001\tcom.apple.driver.thing (1.0/1.0)\tThing\t[activated enabled]\n"
        "*\t*\tTEAMID0002\tcom.vendor.screen (2.0/2.0)\tScreen\t[activated enabled]\n")
    line = sweep.check_mac_extensions()[0]
    check("a third-party system extension is named, and Apple's own is not",
          "com.vendor.screen" in line and "com.apple." not in line, line[:70])
    sweep._run = lambda argv, timeout=30.0: "2 extension(s)\nheader in a new shape\nrow\n"
    line = sweep.check_mac_extensions()[0]
    check("extensions that are there but unreadable are not called clean",
          line.startswith("warn") and "2 system extension" in line, line[:70])
    sweep._run = lambda argv, timeout=30.0: "0 extension(s)\n"
    check("no extensions at all is an ok", sweep.check_mac_extensions()[0].startswith("ok"))

    sweep._run = lambda argv, timeout=30.0: (
        "p123\ncSpotify\nf12\nn127.0.0.1:57621\n"
        "p456\ncmDNSResponder\nf9\nn*:5353\n"
        "p789\ncremotething\nf4\nn*:62078\n")
    line = sweep.check_mac_listeners()[0]
    check("a listener beyond loopback is named with its process",
          line.startswith("warn") and "remotething" in line and "62078" in line, line[:70])
    check("loopback and discovery are left out of it",
          "57621" not in line and "5353" not in line)
    # lsof absent: the fallback is netstat, which is a different shape entirely
    def only_netstat(argv, timeout=30.0):
        if argv[0] == "lsof":
            raise sweep._Fail("lsof is not on this machine")
        return ("tcp4       0      0  *.22                   *.*                    LISTEN\n"
                "tcp4       0      0  127.0.0.1.631          *.*                    LISTEN\n")
    sweep._run = only_netstat
    line = sweep.check_mac_listeners()[0]
    check("with no lsof, netstat answers instead",
          line.startswith("warn") and ":22" in line and "631" not in line, line[:70])

    sweep._run = lambda argv, timeout=30.0: (
        "  1 /sbin/launchd\n 42 /Applications/Safari.app/Contents/MacOS/Safari\n"
        " 77 /tmp/loader\n")
    line = sweep.check_mac_processes()[0]
    check("a process running from a scratch directory is an alert",
          line.startswith("alert") and "77" in line and "/tmp/loader" in line, line[:70])
    sweep._run = lambda argv, timeout=30.0: "  1 /sbin/launchd\n"
    check("and a clean process table says so", sweep.check_mac_processes()[0].startswith("ok"))

    def dead(argv, timeout=30.0):
        raise sweep._Fail("not on this machine")
    sweep._run = dead
    for label, fn in (("gatekeeper", sweep.check_mac_gatekeeper),
                      ("extensions", sweep.check_mac_extensions),
                      ("processes", sweep.check_mac_processes),
                      ("listeners", sweep.check_mac_listeners)):
        line = fn()[0]
        check(f"{label} that cannot run says so instead of looking clean",
              line.startswith("warn"), line[:60])
finally:
    sweep._run = real_run

print("-- the screen, on a Mac --")
real_sys, real_which = vision.sys, vision.shutil.which
vision.sys = types.SimpleNamespace(platform="darwin")
vision.shutil.which = lambda name: "/usr/sbin/screencapture" if name == "screencapture" else None
try:
    check("macOS is looked at with screencapture rather than Pillow",
          vision.backend() == "screencapture")
finally:
    vision.sys, vision.shutil.which = real_sys, real_which

# screencapture insists on a file, so the stand-in writes one: the argument list,
# the file, the read and the encode are all real
real_subprocess = vision.subprocess
PIPE = subprocess.PIPE
shots = []


class FakeProc:
    def __init__(self, code=0, err=b""):
        self.returncode, self.stdout, self.stderr = code, b"", err


def writes_a_frame(colour):
    def fake_run(argv, **kwargs):
        shots.append(list(argv))
        Image.new("RGB", (1600, 900), colour).save(argv[-1])
        return FakeProc()
    return fake_run


vision.subprocess = types.SimpleNamespace(run=writes_a_frame((30, 90, 160)), PIPE=PIPE)
try:
    jpeg = vision._capture_screencapture()
    check("a frame becomes a jpeg", jpeg[:2] == b"\xff\xd8" and len(jpeg) > 1000,
          f"{len(jpeg) / 1024:.0f} kB")
    check("screencapture is asked for silence with -x", "-x" in shots[-1], " ".join(shots[-1]))
    check("and it is asked to write a file, not to a pipe", shots[-1][-1].endswith(".png"))
    check("the temporary file does not outlive the capture",
          not Path(shots[-1][-1]).exists(), shots[-1][-1])
finally:
    vision.subprocess = real_subprocess

vision.subprocess = types.SimpleNamespace(
    run=lambda argv, **kw: FakeProc(1, b"screencapture: cannot run two interactive "
                                       b"screen captures"), PIPE=PIPE)
try:
    try:
        vision._capture_screencapture()
        check("a refusal from screencapture is reported as the permission", False)
    except vision.ScreenError as exc:
        check("a refusal from screencapture is reported as the permission",
              "Screen Recording" in str(exc) and "cannot run two" in str(exc), str(exc)[:60])
finally:
    vision.subprocess = real_subprocess

vision.subprocess = types.SimpleNamespace(run=writes_a_frame((0, 0, 0)), PIPE=PIPE)
try:
    try:
        vision._capture_screencapture()
        check("a black frame is reported as the permission, not handed to the model", False)
    except vision.ScreenError as exc:
        check("a black frame is reported as the permission, not handed to the model",
              "black" in str(exc) and "Screen Recording" in str(exc), str(exc)[:60])
finally:
    vision.subprocess = real_subprocess

print("-- the microphone, in words that help --")
real_audio_sys = audio.sys
try:
    for platform, expected in (("darwin", "Microphone permission"),
                               ("win32", "privacy setting"),
                               ("linux", "--input")):
        audio.sys = types.SimpleNamespace(platform=platform)
        said = audio.open_error(OSError("device unavailable"))
        check(f"a {platform} microphone failure points at the {expected}",
              expected in said and "device unavailable" in said, said[:55])
finally:
    audio.sys = real_audio_sys

print("-- the dashboard window, on a Mac --")
real_dirs = main_mod.MAC_APP_DIRS
apps = workdir("applications")
firefox = apps / "Firefox.app" / "Contents/MacOS/firefox"
firefox.parent.mkdir(parents=True)
firefox.write_text("#!/bin/sh\n")
firefox.chmod(0o755)
main_mod.MAC_APP_DIRS = (apps,)
try:
    found = main_mod._mac_browser()
    check("a browser is found where macOS puts browsers",
          found is not None and found[0] == "Firefox", str(found[0]) if found else "None")
    check("and it is known to want --new-window rather than --app",
          found is not None and found[2] is True)
    brave = apps / "Brave Browser.app" / "Contents/MacOS/Brave Browser"
    brave.parent.mkdir(parents=True)
    brave.write_text("#!/bin/sh\n")
    brave.chmod(0o755)
    found = main_mod._mac_browser()
    check("the list is in preference order, so Brave wins over Firefox",
          found is not None and found[0] == "Brave Browser", str(found[0]) if found else "None")
finally:
    main_mod.MAC_APP_DIRS = real_dirs

for path in TEMPS:
    shutil.rmtree(path, ignore_errors=True)

print()
print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)"
      + (f", {skipped} skipped" if skipped else ""))
sys.exit(1 if failed else 0)
