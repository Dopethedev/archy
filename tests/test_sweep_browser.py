"""The browser and launcher checks, each against a planted copy.

Nothing here touches the live Brave profile or the live launcher directories:
every scenario is built in a temporary tree, the way malware would actually
appear, and the sweep is pointed at the copy. The live run in run_all is the
reverse: the checks must stay quiet on a clean machine.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import jarvis.sweep as sweep
from jarvis.sweep import check_browser, check_launchers

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


def fake_brave(root: Path, profile_prefs: dict, components: dict):
    """A Brave tree: profiles as Preferences files, components as id/ver dirs."""
    for prof, settings in profile_prefs.items():
        prefs = root / prof
        prefs.mkdir(parents=True)
        (prefs / "Preferences").write_text(
            json.dumps({"extensions": {"settings": settings}}))
    for comp_dir, (version, manifest) in components.items():
        d = root / comp_dir / version
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps(manifest))


def ext_setting(location, webstore, path, version="1.0.0"):
    return {"location": location, "from_webstore": webstore, "path": path}


print("-- browser: what each shape of trouble looks like --")
with tempfile.TemporaryDirectory() as raw:
    base = Path(raw)
    fake_brave(base,
               {"Default": {
                   "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":   # clean webstore ext
                       ext_setting(1, True, "1.0.0"),
                   "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb":   # unpacked, dropped
                       ext_setting(4, False, "1.0.0"),
                   "cccccccccccccccccccccccccccccccc":   # store ext, greedy
                       ext_setting(1, True, "1.0.0"),
               }},
               {"dddddddddddddddddddddddddddddddd":       # component, owned
                   ("1.0.0", {"name": "Some Component"})})
    for ext, manifest in (
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
         {"name": "Fine Helper", "permissions": ["storage"]}),
        ("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
         {"name": "Sneaky Helper", "permissions": ["tabs"]}),
        ("cccccccccccccccccccccccccccccccc",
         {"name": "Greedy Helper",
          "host_permissions": ["<all_urls>"], "permissions": ["cookies"]}),
    ):
        d = base / "Default" / "Extensions" / ext / "1.0.0"
        d.mkdir(parents=True)
        m = d / "manifest.json"
        m.write_text(json.dumps(manifest))
        old = time.time() - 30 * 86400
        os.utime(m, (old, old))
    real_owners = sweep._pacman_owners
    sweep._pacman_owners = lambda paths: {
        p: ("brave-bin 1.0" if "dddddddd" in p else None) for p in paths}
    try:
        out = check_browser(base)
        check("an unpacked extension is an alert",
              any("Sneaky Helper" in f and f.startswith("alert") for f in out))
        check("a greedy store extension is a warning with the reason",
              any("Greedy Helper" in f and "every site" in f for f in out))
        check("tabs alone is named too",
              any("Sneaky Helper" in f and "sees every tab" in f for f in out))
        check("a clean webstore extension draws no line",
              not any("Fine Helper" in f for f in out))
        check("an owned component draws no line",
              not any("Some Component" in f for f in out))
        check("the binary verification line exists",
              any("brave binary" in f for f in out))
        check("no flash plugin is stated", any("no flash plugin" in f for f in out))
    finally:
        sweep._pacman_owners = real_owners

print("-- browser: recent install, unowned component, altered binary --")
with tempfile.TemporaryDirectory() as raw:
    base = Path(raw)
    fake_brave(base,
               {"Default": {
                   "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee": ext_setting(1, True, "1.0.0"),
               }},
               {"ffffffffffffffffffffffffffffffff":
                   ("2.0.0", {"name": "Planted Component"})})
    d = base / "Default" / "Extensions" / "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" / "1.0.0"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps(
        {"name": "Newcomer", "permissions": ["storage"]}))
    real_owners, real_verify = sweep._pacman_owners, sweep._pacman_verify
    sweep._pacman_owners = lambda paths: {p: None for p in paths}
    sweep._pacman_verify = lambda pkg: (
        "brave-bin: 279 total files, 0 altered files" if "0" in pkg
        else "brave-bin: /usr/bin/brave checksum FAILED, altered file")
    try:
        out = check_browser(base)
        check("an unowned component is an alert",
              any("Planted Component" in f and f.startswith("alert") for f in out),
              "; ".join(f for f in out if f.startswith("alert")))
        check("a two-day-old install is a warning",
              any("Newcomer" in f and "last two days" in f for f in out))
        sweep._pacman_verify = lambda pkg: "brave-bin: 1 altered file: /usr/bin/brave"
        out = check_browser(base)
        check("an altered brave binary is an alert",
              any(f.startswith("alert") and "brave binary" in f for f in out))
    finally:
        sweep._pacman_owners, sweep._pacman_verify = real_owners, real_verify

print("-- browser: the real profile on this machine stays quiet --")
out = check_browser()
check("live profile draws no alert",
      not any(f.startswith("alert") for f in out),
      "; ".join(out[:3]))

SIM = Path(__file__).resolve().parent / "sim"


def temp_ground():
    """A scratch area for planted persistence that is not itself in /tmp, so
    the scratch-path rule cannot fire on the test's own fixtures."""
    return tempfile.TemporaryDirectory(dir=SIM)


print("-- launchers: planted persistence, on copies --")
with temp_ground() as raw:
    root = Path(raw)
    sysapps = root / "sys" / "applications"
    sysauto = root / "sys" / "autostart"
    userapps = root / "user" / "applications"
    userauto = root / "user" / "autostart"
    scratch = root / "tmp"
    for d in (sysapps, sysauto, userapps, userauto, scratch):
        d.mkdir(parents=True)
    (scratch / "dropper").write_text("#!/bin/sh\n")
    dropper = scratch / "dropper"
    dropper.chmod(0o755)
    legit = root / "real-tool"
    legit.write_text("#!/bin/sh\n")
    legit.chmod(0o755)
    old_user_tool = root / "user-tool"
    old_user_tool.write_text("#!/bin/sh\n")
    old_user_tool.chmod(0o755)
    old_t = time.time() - 90 * 86400
    os.utime(old_user_tool, (old_t, old_t))
    (sysapps / "good.desktop").write_text(
        "[Desktop Entry]\nType=Application\nExec=/usr/bin/real-tool\n")
    (sysapps / "stolen.desktop").write_text(
        "[Desktop Entry]\nType=Application\nExec=/usr/bin/not-a-real-thing\n")
    # a /usr/local path, the ground local scripts live on by design. A
    # nonexistent one is used so the live system is never touched; the
    # fresh-vs-old split itself is covered by the user launcher pair above.
    (sysapps / "local.desktop").write_text(
        "[Desktop Entry]\nType=Application\nExec=/usr/local/bin/nobody-owns-this\n")
    (sysauto / "evil.desktop").write_text(
        f"[Desktop Entry]\nType=Application\nExec={dropper}\n")
    (userapps / "mine.desktop").write_text(
        f"[Desktop Entry]\nType=Application\nExec={old_user_tool}\n")
    (userauto / "fresh.desktop").write_text(
        f"[Desktop Entry]\nType=Application\nExec={legit}\n")
    real_owners, real_last, real_scan = (sweep._pacman_owners,
                                         sweep._last_pacman_change, sweep._clamav_files)
    sweep._pacman_owners = lambda paths: {
        p: ("coreutils 9.0" if p == "/usr/bin/real-tool" else None) for p in paths}
    sweep._last_pacman_change = lambda: time.time() - 30 * 86400
    sweep._clamav_files = lambda paths, what: [
        f"ok: fake scan of {len(paths)} {what}(s)"]
    try:
        dirs = (("system applications", sysapps, True),
                ("system autostart", sysauto, True),
                ("user applications", userapps, False),
                ("user autostart", userauto, False))
        out = check_launchers(dirs)
        check("an autostart entry running from /tmp is an alert",
              any("evil.desktop" in f and "scratch" in f and f.startswith("alert")
                  for f in out))
        check("an unowned system launcher is an alert",
              any("stolen.desktop" in f and f.startswith("alert") for f in out))
        check("an unowned recent user launcher warns",
              any("fresh.desktop" in f and f.startswith("warn") and
                  "appeared after the last install" in f for f in out))
        check("an old unowned user launcher is only an info line",
              any("mine.desktop" in f and f.startswith("info") for f in out)
              and not any("mine.desktop" in f and f.startswith("warn") for f in out))
        check("an owned system launcher draws no line",
              not any("good.desktop" in f for f in out))
        check("an unowned /usr/local script is judged by age, not alerted",
              not any("local.desktop" in f and f.startswith("alert") for f in out)
              and any("local.desktop" in f and f.startswith("info") for f in out))
        check("unowned targets went to the scanner",
              any("fake scan" in f and "launcher target" in f for f in out))
    finally:
        sweep._pacman_owners, sweep._last_pacman_change = real_owners, real_last
        sweep._clamav_files = real_scan

print("-- launchers: field codes, env wrappers, TryExec, user services --")
with temp_ground() as raw:
    root = Path(raw)
    apps = root / "a"
    services = root / "s"
    apps.mkdir(); services.mkdir()
    (apps / "codes.desktop").write_text(
        "[Desktop Entry]\nType=Application\nExec=env FOO=bar /bin/ls %U\n")
    (apps / "commented.desktop").write_text(
        "[Desktop Entry]\nType=Application\n# Exec=/tmp/nope\nExec=/bin/true\n")
    (services / "unit.service").write_text(
        "[Service]\nExecStart=/tmp/sneak %f\n")
    real_owners, real_scan = sweep._pacman_owners, sweep._clamav_files
    sweep._pacman_owners = lambda paths: {p: None for p in paths}
    sweep._clamav_files = lambda paths, what: [f"ok: fake scan {len(paths)}"]
    try:
        out = check_launchers((("user applications", apps, False),),
                              unit_dir=services)
        check("field codes do not confuse the target",
              any("codes.desktop" in f and "/bin/ls" in f for f in out))
        check("a commented-out Exec is ignored",
              not any("/tmp/nope" in f for f in out))
        check("a user service ExecStart from /tmp is an alert",
              any("unit.service" in f and "scratch" in f for f in out))
    finally:
        sweep._pacman_owners, sweep._clamav_files = real_owners, real_scan

print("-- launchers: the live machine stays calm --")
real_scan = sweep._clamav_files
sweep._clamav_files = lambda paths, what: [
    f"ok: live scan skipped here ({len(paths)} target(s))"]
try:
    out = check_launchers()
    check("no alert on the live machine",
          not any(f.startswith("alert") for f in out),
          "; ".join(out[:3]))
finally:
    sweep._clamav_files = real_scan

print("-- both land in the sweep's report --")
from jarvis.sweep import run
report = run()
check("the report mentions the browser", "brave" in report.lower()
      or "extension" in report.lower())
check("the report mentions launchers", "launcher" in report.lower())
check("the summary line is still last",
      report.rstrip().splitlines()[-1].strip().startswith("summary:"))

import shutil
if SIM.exists():
    for child in SIM.iterdir():
        shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()

print()
print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
sys.exit(0 if not failed else 1)
