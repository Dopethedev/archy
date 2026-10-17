"""The security sweep.

Concrete checks, run locally, no model in the loop until the results exist:

- clamav over the places malware actually lands first: /tmp, /dev/shm and
  ~/Downloads. A whole-disk scan would take hours and find nothing that a
  targeted one would; the scope is stated in the report so nobody mistakes it
  for everything.
- processes whose executable lives in /tmp or /dev/shm, or has been deleted
  from disk while still running: the classic shape of a dropped and unlinked
  payload.
- accounts with uid 0 other than root: the oldest backdoor there is.
- /etc/hosts lines that steer domains somewhere else.
- world-writable executable files in the shared memory directories.
- what is listening on the network, and ssh's exposed state.
- user systemd units that launch things from /tmp or hidden directories.

Every check reports, even when clean. Silence is how a skipped check hides.
"""
import os
import pwd
import re
import subprocess
import time
from pathlib import Path

from . import config

HOME = Path.home()
HOSTS_PATH = Path("/etc/hosts")


class _Fail(Exception):
    pass


def _run(argv: list[str], timeout: float = 30.0) -> str:
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise _Fail(str(exc)) from None
    return proc.stdout.decode(errors="replace")


def _proc_entries():
    """(pid, exe path or None if deleted/unreadable, cmdline) for every process."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        exe = Path(f"/proc/{pid}/exe")
        try:
            target = os.readlink(exe)                     # " (deleted)" suffix if gone
        except (PermissionError, FileNotFoundError, OSError):
            target = None
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except (PermissionError, FileNotFoundError, OSError):
            cmdline = ""
        yield pid, target, cmdline


def check_processes() -> list[str]:
    findings = []
    for pid, exe, cmdline in _proc_entries():
        if exe is None:
            continue                                      # kernel threads and unreadable
        low = exe.lower()
        if "/tmp/" in low or "/dev/shm/" in low:
            findings.append(f"alert: pid {pid} runs from {exe}: {cmdline[:80]}")
        elif exe.endswith("(deleted)"):
            # normal right after a package upgrade, suspicious otherwise, so it
            # is a warning to read, not an alert to act on
            findings.append(f"warn: pid {pid} runs a deleted file: {cmdline[:80]}")
    return findings or ["ok: no process runs from /tmp, /dev/shm or a deleted file"]


def check_uid_zero() -> list[str]:
    roots = [p.pw_name for p in pwd.getpwall() if p.pw_uid == 0]
    if roots == ["root"]:
        return ["ok: root is the only account with uid 0"]
    return [f"alert: these accounts have uid 0: {', '.join(roots)}"]


def check_hosts(path: Path = None) -> list[str]:
    try:
        lines = (path or HOSTS_PATH).read_text(errors="replace").splitlines()
    except OSError as exc:
        return [f"warn: /etc/hosts unreadable: {exc}"]
    # loopback, link-local, ipv6 multicast and the hostname mapping are all
    # standard; flagging them taught the report to cry wolf
    normal = re.compile(r"^(127\.|::1\b|fe80|ff[0-9a-f]{2}::|0\.0\.0\.0\b)", re.IGNORECASE)
    odd = [ln.strip() for ln in lines
           if ln.strip() and not ln.strip().startswith("#") and not normal.match(ln.strip())]
    if not odd:
        return ["ok: /etc/hosts holds only standard entries"]
    return [f"warn: /etc/hosts maps these, check they are yours: {'; '.join(odd[:6])}"]


def check_world_writable(bases: tuple = ("/tmp", "/dev/shm")) -> list[str]:
    findings = []
    for base in bases:
        root = Path(base)
        if not root.exists():
            continue
        for path in list(root.rglob("*"))[:2000]:
            try:
                if path.is_file() and os.access(path, os.X_OK) \
                        and (os.stat(path).st_mode & 0o002):
                    findings.append(f"alert: world-writable executable {path}")
            except OSError:
                continue
    return findings or ["ok: no world-writable executables in /tmp or /dev/shm"]


def check_listeners() -> list[str]:
    try:
        out = _run(["ss", "-tuln"])
    except _Fail as exc:
        return [f"warn: could not list listeners: {exc}"]
    # what a normal desktop runs: the resolver stubs, mDNS discovery, LLMNR and
    # the dhcp client. a sweep that flags those teaches its reader to ignore it.
    discovery = re.compile(r":(5353|5355|546)(%\w+)?$")
    odd = set()
    for ln in out.splitlines()[1:]:
        parts = ln.split()
        if len(parts) < 5:
            continue
        addr = parts[4].split("%", 1)[0]
        if addr.startswith(("127.", "[::1]")):
            continue
        if discovery.search(parts[4]):
            continue
        odd.add(parts[4])
    if not odd:
        return ["ok: nothing listening beyond loopback and discovery services"]
    return [f"warn: listening beyond loopback: {', '.join(sorted(odd)[:10])}"]


def check_ssh() -> list[str]:
    findings = []
    keys = HOME / ".ssh" / "authorized_keys"
    if keys.exists():
        try:
            entries = [ln for ln in keys.read_text(errors="replace").splitlines()
                       if ln.strip() and not ln.startswith("#")]
            findings.append(f"info: {len(entries)} authorized ssh key(s); "
                            f"confirm you know each one")
        except OSError as exc:
            findings.append(f"warn: authorized_keys unreadable: {exc}")
    else:
        findings.append("ok: no authorized ssh keys")
    return findings


def check_user_units() -> list[str]:
    findings = []
    unit_dirs = [HOME / ".config/systemd/user"]
    for base in unit_dirs:
        if not base.exists():
            continue
        for unit in base.glob("*.service"):
            try:
                body = unit.read_text(errors="replace")
            except OSError:
                continue
            for line in body.splitlines():
                if line.strip().startswith(("ExecStart", "ExecStartPre", "ExecStartPost")):
                    cmd = line.split("=", 1)[-1]
                    low = cmd.lower()
                    if "/tmp/" in low or "/dev/shm/" in low or "/.cache/" in low:
                        findings.append(f"alert: {unit.name} launches from {cmd.strip()[:80]}")
    return findings or ["ok: no user service launches from a scratch directory"]


def check_updates() -> list[str]:
    try:
        out = _run(["pacman", "-Qu"], timeout=60)
    except _Fail:
        return ["info: could not count pending updates"]
    n = len([ln for ln in out.splitlines() if ln.strip()])
    return [f"info: {n} package update(s) pending; old packages carry known holes"]


def check_clamav() -> list[str]:
    findings = []
    db = Path("/var/lib/clamav")
    # daily updates arrive as a .cld, the big databases as .cvd: checking only
    # one kind reported a month-old database that was in fact current
    newest = 0.0
    for cvd in list(db.glob("*.cvd")) + list(db.glob("*.cld")):
        newest = max(newest, cvd.stat().st_mtime)
    if not newest:
        return ["warn: no clamav virus database found"]
    age = (time.time() - newest) / 86400
    tag = "ok" if age < 2 else "warn"
    findings.append(f"{tag}: virus definitions are {age:.1f} day(s) old")
    targets = ["/tmp", "/dev/shm", str(HOME / "Downloads")]
    existing = [t for t in targets if Path(t).exists()]
    argv = ["clamscan", "--no-summary", "--infected", "--recursive",
            "--max-filesize=100M", "--max-scansize=400M", *existing]
    try:
        started = time.time()
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=config.SWEEP_CLAMAV_TIMEOUT_S)
        out = proc.stdout.decode(errors="replace")
    except subprocess.TimeoutExpired:
        return findings + [f"warn: the scan of {', '.join(existing)} hit the "
                           f"{config.SWEEP_CLAMAV_TIMEOUT_S:.0f}s limit partway; partial sweep only"]
    except FileNotFoundError:
        return findings + ["warn: clamscan is not installed"]
    hits = [ln for ln in out.splitlines() if "FOUND" in ln]
    if hits:
        findings += [f"alert: clamav flagged {ln.strip()[:120]}" for ln in hits[:8]]
    else:
        findings.append(f"ok: clamav scanned {', '.join(existing)} in "
                        f"{time.time()-started:.0f}s and found nothing")
    return findings


CHECKS = (check_processes, check_uid_zero, check_hosts, check_world_writable,
          check_listeners, check_ssh, check_user_units, check_updates, check_clamav)


def wanted(text: str) -> bool:
    """Does this request ask for a sweep? Shares the trigger words with the
    assistant, which calls the checks and hands the report to the model."""
    words = text.lower()
    return any(w in words for w in config.SWEEP_WORDS)


def run() -> str:
    """Run every check and return a compact plain-text report for the model."""
    lines = [f"security sweep of {os.uname().nodename}, {time.strftime('%H:%M')}:"]
    for check in CHECKS:
        try:
            lines += [f"  {f}" for f in check()]
        except Exception as exc:                      # a check must never sink the sweep
            lines.append(f"  warn: {check.__name__} failed: {exc}")
    alerts = sum(1 for ln in lines if "alert:" in ln)
    warns = sum(1 for ln in lines if ln.strip().startswith("warn:"))
    verdict = "nothing needs action" if not alerts else f"{alerts} thing(s) need action"
    lines.append(f"  summary: {verdict}, {warns} warning(s) worth reading")
    report = "\n".join(lines)
    return report if len(report) < 3800 else report[:3700] + "\n  (report truncated)"
