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
    async def ask(self, text, images=None):
        self.seen.append((text, images))
        yield "done", "clean"


class SweepHarness:
    def __init__(self):
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
    sweep.run = lambda: report
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

    h = await run_turn("what time is it", "should not run")
    check("an ordinary question runs no sweep", h.brain.seen and "SWEEP" not in h.brain.seen[0][0])

    print()
    print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
