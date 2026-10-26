"""The security sweep: each check against crafted ground, and the wiring.

The real sweep is verified by running it (run_all does not, it needs clamav and
a live compositor); these tests prove each check can tell a threat from
desktop noise, because a sweep that cries wolf trains its reader to ignore it.
"""
import asyncio
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import jarvis.assistant as jarvis_assistant
from jarvis import status as status_mod
from jarvis import privacy
import jarvis.sweep as sweep
from jarvis.assistant import Assistant
from jarvis.sweep import (check_hosts, check_listeners, check_processes,
                          check_uid_zero, check_world_writable, run, wanted)

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


print("-- what triggers the sweep --")
for text in ("run a security sweep", "do a virus scan", "am i infected",
             "check my computer for malware", "any viruses on my machine"):
    check(f"wanted {text!r}", wanted(text))
for text in ("what time is it", "open brave", "how much ram is free",
             "scan the qr code", "remember my birthday"):
    check(f"not wanted {text!r}", not wanted(text))

print("-- processes: the classic drops --")
real = sweep._proc_entries
sweep._proc_entries = lambda: [
    ("100", "/usr/bin/firefox", "firefox"),
    ("101", "/tmp/.x/process (deleted)", "hidden payload"),
    ("102", "/dev/shm/miner", "kworker88"),
    ("103", "/usr/lib/systemd/systemd (deleted)", "systemd upgraded underneath us"),
]
try:
    out = check_processes()
    check("a process from /tmp is an alert", any("pid 101" in f and "alert" in f for f in out))
    check("a process from /dev/shm is an alert", any("pid 102" in f and "alert" in f for f in out))
    check("an upgraded system binary warns but is not an alert",
          any("pid 103" in f and f.startswith("warn") for f in out)
          and not any("pid 103" in f and f.startswith("alert") for f in out))
    check("ordinary processes stay unnamed", all("firefox" not in f for f in out))
finally:
    sweep._proc_entries = real

print("-- accounts --")
real_pwall = sweep.pwd.getpwall
sweep.pwd.getpwall = lambda: [
    type("P", (), {"pw_name": "root", "pw_uid": 0})(),
    type("P", (), {"pw_name": "dope", "pw_uid": 1000})(),
]
try:
    check("root alone is clean", check_uid_zero()[0].startswith("ok"))
    sweep.pwd.getpwall = lambda: [
        type("P", (), {"pw_name": "root", "pw_uid": 0})(),
        type("P", (), {"pw_name": "toor", "pw_uid": 0})(),
    ]
    out = check_uid_zero()
    check("a second uid 0 is an alert", out[0].startswith("alert") and "toor" in out[0])
finally:
    sweep.pwd.getpwall = real_pwall

print("-- hosts: hijacks yes, desktop noise no --")
with tempfile.NamedTemporaryFile("w", suffix="hosts", delete=False) as fh:
    fh.write("127.0.0.1 localhost\n::1 localhost\n"
             "ff02::1 ip6-allnodes\nff02::2 ip6-allrouters\n"
             "127.0.1.1 Archy\n0.0.0.0 telemetry.example.com\n")
    clean = fh.name
out = check_hosts(Path(clean))
check("multicast and hostname mapping are not flagged", out[0].startswith("ok"), out[0])
with tempfile.NamedTemporaryFile("w", suffix="hosts", delete=False) as fh:
    fh.write("127.0.0.1 localhost\n104.21.6.42 my-bank.example.com\n")
    hijack = fh.name
out = check_hosts(Path(hijack))
check("a bank mapped to a foreign address is flagged", out[0].startswith("warn"))
os.unlink(clean); os.unlink(hijack)

print("-- listeners: discovery services no, the rest yes --")
real_run = sweep._run
sample = """Netid State Recv-Q Send-Q Local Address:Port Peer
udp  UNCONN 0     0      127.0.0.53%lo:53 0.0.0.0:*
udp  UNCONN 0     0      127.0.0.54:53    0.0.0.0:*
udp  UNCONN 0     0      0.0.0.0:5353     0.0.0.0:*
udp  UNCONN 0     0      0.0.0.0:5355     0.0.0.0:*
udp  UNCONN 0     0      [::]:546         [::]:*
tcp  LISTEN  0     0      127.0.0.1:8731   0.0.0.0:*
"""
sweep._run = lambda argv, timeout=30: sample
try:
    out = check_listeners()
    check("mDNS, LLMNR, dhcpv6 are not flagged", out[0].startswith("ok"), out[0][:60])
    sweep._run = lambda argv, timeout=30: sample + "tcp  LISTEN  0  0  0.0.0.0:9999 0.0.0.0:*\n"
    out = check_listeners()
    check("an odd port on all interfaces is flagged",
          out[0].startswith("warn") and "9999" in out[0])
finally:
    sweep._run = real_run

print("-- world-writable executables --")
with tempfile.TemporaryDirectory() as base:
    normal = Path(base) / "script"
    normal.write_text("#!/bin/sh\n")
    normal.chmod(0o755)
    out = check_world_writable(bases=(base,))
    check("an ordinary script is not flagged", out[0].startswith("ok"))
    trap = Path(base) / "dropper"
    trap.write_text("#!/bin/sh\n")
    trap.chmod(0o777)
    out = check_world_writable(bases=(base,))
    check("a world-writable executable is an alert", out[0].startswith("alert"))

print("-- the whole sweep never crashes, whatever a check does --")
def _broken():
    raise RuntimeError("broken")
real_checks = sweep.CHECKS
idx = real_checks.index(sweep.check_updates)
sweep.CHECKS = real_checks[:idx] + (_broken,) + real_checks[idx + 1:]
try:
    report = run()
    check("a broken check is reported, sweep continues", "broken" in report)
finally:
    sweep.CHECKS = real_checks

print("-- the turn runs it and hands the report to the model --")
class FakeBrain:
    def __init__(self):
        self.seen = []
    def model_name(self):
        return "some-relay/large-model"      # a remote model, so the switches apply
    async def ask(self, text, images=None):
        self.seen.append((text, images))
        yield "done", "clean"


class SweepHarness:
    def __init__(self):
        self._privacy_confirm = privacy.Confirmation()
        self.status = status_mod.Feed()
        self.spoken = []
        self.log_lines = []
        self.brain = FakeBrain()
        self.speaker = self
        self.player = type("P", (), {"wait_drained": lambda s, t=None: None})()
        self.mic_enabled = True
        self.state = "thinking"
        self.hud = None
    def say_stream(self, deltas, on_first, cancel):
        for item in deltas:
            self.spoken.append(item)
    def say(self, text):
        self.spoken.append(text)
    _theme_intent = staticmethod(Assistant._theme_intent)
    def log(self, who, text):
        self.log_lines.append(text)
    def _publish_heard(self, text, partial=False):
        pass
    def set_state(self, s):
        self.state = s
    def _back_to_idle(self):
        self.set_state("idle")


async def run_turn(text, report):
    h = SweepHarness()
    real = sweep.run
    h.deep = None
    sweep.run = lambda deep=False: (h.__setattr__("deep", deep), report)[1]
    try:
        await Assistant._process(h, text=text)
    finally:
        sweep.run = real
    return h


async def main():
    h = await run_turn("run a security sweep",
                       "  ok: nothing bad found\n  warn: definitions are 30.7 day(s) old")
    check("the announcement is spoken before anything else",
          h.spoken and "Running the sweep" in h.spoken[0])
    check("the report reaches the model",
          h.brain.seen and "SECURITY SWEEP RESULTS" in h.brain.seen[0][0]
          and "definitions are 30.7" in h.brain.seen[0][0])
    check("no images on a sweep turn", h.brain.seen and h.brain.seen[0][1] is None)
    check("an ordinary sweep does not ask for the deeper checks", h.deep is False, str(h.deep))

    h = await run_turn("run a deep sweep", "  ok: nothing bad found")
    check("a deep sweep is announced as the slower one",
          h.spoken and "deeper check" in h.spoken[0], str(h.spoken[:1]))
    check("and the checks that take minutes are the ones it runs", h.deep is True, str(h.deep))

    h = await run_turn("what time is it", "should not run")
    check("an ordinary question runs no sweep", h.brain.seen and "SWEEP" not in h.brain.seen[0][0])


    # A security check must never read as clean because it did not run. These drive
    # the result parsing directly, since the point is the claim the report makes.

    import subprocess as _sp


    def _fake(returncode, stderr="", stdout=""):
        return _sp.CompletedProcess(["clamscan"], returncode, stdout.encode(),
                                    stderr.encode())


    hits, error, skipped = sweep._clamav_result(
        _fake(1, stdout="/tmp/x.bin: Eicar-Test-Signature FOUND\n"))
    check("a finding is a finding", hits and not error, str(hits))

    hits, error, skipped = sweep._clamav_result(_fake(0))
    check("a clean exit is clean", not hits and not error and not skipped)

    # the daemon being down: this is the case that used to read as clean
    hits, error, skipped = sweep._clamav_result(
        _fake(2, stderr="ERROR: Could not connect to clamd on LocalSocket ...\n"))
    check("a scan that could not run is not reported as clean",
          not hits and "Could not connect" in error, error[:60])

    # ...and the case that must not cry wolf: warnings only, scan ran fine
    hits, error, skipped = sweep._clamav_result(
        _fake(2, stderr="WARNING: /tmp/sock: Not supported file type\n"
                        "WARNING: /tmp/locked: Permission denied\n"))
    check("skipped files are not a failed scan", not hits and not error and skipped == 2,
          f"skipped {skipped}")

    print("-- only real files are offered to the scanner --")
    work = Path(tempfile.mkdtemp(prefix="jarvis-clam-"))
    (work / "a_regular_file").write_text("hello\n")
    os.symlink("/etc/hosts", work / "a_symlink")
    import socket as _socket
    sock = _socket.socket(_socket.AF_UNIX)
    sock.bind(str(work / "a_socket"))
    try:
        files, seen, total = sweep._newest_files([str(work)], limit=50)
        names = [Path(f).name for f in files]
        check("a regular file is included", "a_regular_file" in names, str(names))
        check("a socket is not", "a_socket" not in names, str(names))
        check("counted, though: the report says how many it saw", seen >= 3, str(seen))
    finally:
        sock.close()

    print("-- and the cap is a cap, with the arithmetic stated --")
    for i in range(30):
        (work / f"f{i:02d}").write_text("x")
    files, seen, total = sweep._newest_files([str(work)], limit=10)
    check("the limit is honoured", len(files) == 10, str(len(files)))
    check("everything seen is counted", seen >= 33, str(seen))
    check("the newest are chosen", Path(files[0]).name == "f29", Path(files[0]).name)
    (work / "f29").unlink()
    check("a missing file never breaks the walk",
          sweep._newest_files([str(work / "does-not-exist")], limit=5)[0] == [])

    # a platform the checks know nothing about says so, rather than reporting a
    # screenful of "unavailable" that reads like a clean sweep
    real_supported = sweep.supported
    sweep.supported = lambda: False
    try:
        line = sweep.run()
        check("another platform is told so, not swept",
              "neither Linux nor macOS" in line and len(line.splitlines()) == 1, line[:70])
    finally:
        sweep.supported = real_supported
    check("and this platform is swept", sweep.supported() and "security sweep of" in sweep.run())

    print()
    print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

