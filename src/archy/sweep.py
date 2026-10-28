"""The security sweep.

Concrete checks, run locally, no model in the loop until the results exist.
On Linux:

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
- the browser: what is loaded into Brave, where it came from, and what it may
  see; plus the binary and any flash plugin verified against pacman's records.
- launchers: desktop entries, autostart files and user services, checked for
  scratch directories, missing package ownership, and files that appeared
  without any install; suspicious targets go through clamav as well.

On macOS, where there is no /proc, no systemd and no package manager, the same
questions are asked of the switches a Mac actually has: what launchd starts at
login and whether the program it starts is signed, which system extensions are
installed, whether Gatekeeper is on, what is listening, and /etc/hosts.

Every check reports, even when clean. Silence is how a skipped check hides.

Most of it is a Linux machine's business, so the two things that only exist on
Unix are imported where they are used rather than at the top: `pwd` here, and the
package manager and `systemctl` inside their own checks. Importing them at module
level meant that on Windows, where the sweep has nothing to do anyway, the
assistant could not even start.
"""
import itertools
import json
import os
import platform
import plistlib
import re

try:
    import pwd
except ImportError:              # Windows: no account database to read
    pwd = None                   # type: ignore[assignment]
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config

HOME = Path.home()
HOSTS_PATH = Path("/etc/hosts")
BRAVE_BASE = HOME / ".config/BraveSoftware/Brave-Browser"
LAUNCHER_DIRS = (
    ("system applications", Path("/usr/share/applications"), True),
    ("system autostart", Path("/etc/xdg/autostart"), True),
    ("user applications", HOME / ".local/share/applications", False),
    ("user autostart", HOME / ".config/autostart", False),
)
SYSTEMD_USER_DIR = HOME / ".config/systemd/user"
# what an extension may ask for, and why each one matters in plain speech
PERM_REASONS = (
    ("<all_urls>", "reads and runs code on every site"),
    ("http://*/*", "reads every http site"),
    ("https://*/*", "reads every site"),
    ("*://*/*", "reads every site"),
    ("tabs", "sees every tab"),
    ("webRequest", "watches your web traffic"),
    ("cookies", "reads cookies"),
    ("history", "reads your history"),
    ("debugger", "has debug control of pages"),
    ("nativeMessaging", "talks to native programs"),
    ("clipboardRead", "reads the clipboard"),
    ("management", "can manage other extensions"),
    ("proxy", "can reroute your traffic"),
)
# the scratch locations. /run/user because a userland payload can write there
# without root; bare /run/ is left out, a user's own ~/runner would match it.
SCRATCH_PARTS = ("/tmp/", "/dev/shm/", "/var/tmp/", "/run/user/", "/.cache/")


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
    unreadable = 0
    readable = 0
    for pid, exe, cmdline in _proc_entries():
        if exe is None:
            # kernel threads, and every process belonging to someone else: this
            # user cannot readlink their exe. Counting them matters, because the
            # finding below says no process runs from a scratch directory, and that
            # is a claim about the processes this check could see.
            unreadable += 1
            continue
        readable += 1
        low = exe.lower()
        if "/tmp/" in low or "/dev/shm/" in low:
            findings.append(f"alert: pid {pid} runs from {exe}: {cmdline[:80]}")
        elif exe.endswith("(deleted)"):
            # normal right after a package upgrade, suspicious otherwise, so it
            # is a warning to read, not an alert to act on
            findings.append(f"warn: pid {pid} runs a deleted file: {cmdline[:80]}")
    if findings:
        return findings
    if unreadable:
        # counted while walking, not by walking again: a second pass is a second
        # sample of a process list that changes as it is read, so the number it
        # printed need not be the number this loop just saw
        return [f"ok: none of the {readable} readable process(es) runs from /tmp, "
                f"/dev/shm or a deleted file; {unreadable} could not be read "
                f"(kernel threads, or another user's processes)"]
    return ["ok: no process runs from /tmp, /dev/shm or a deleted file"]


def check_uid_zero() -> list[str]:
    if pwd is None:                  # a platform without an accounts file
        return ["ok: no account database here, nothing to compare"]
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
        # islice, not a slice of a list: `list(rglob())` walks the whole tree
        # before the cap is applied, so the cap bounded what was reported and not
        # the work, on the one directory on this machine with ten thousand files
        for path in itertools.islice(root.rglob("*"), 2000):
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


def _pacman_verify(pkg: str) -> str:
    """pacman -Qkk: the checksums recorded at install time, re-run now."""
    try:
        proc = subprocess.run(["pacman", "-Qkk", pkg],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise _Fail(str(exc)) from None
    return (proc.stdout + proc.stderr).decode(errors="replace")


def _pacman_owners(paths: list[str]) -> dict:
    """Which package owns each path, in one pacman call. None means unowned."""
    if not paths:
        return {}
    try:
        proc = subprocess.run(["pacman", "-Qo", *paths],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise _Fail(str(exc)) from None
    owners = {}
    for ln in (proc.stdout + proc.stderr).decode(errors="replace").splitlines():
        m = re.match(r"^(.+?) is owned by ", ln)
        if m:
            owners[m.group(1)] = ln.split("is owned by ", 1)[1]
        m = re.match(r"error: (?:No package owns|no package owns) (.+)$", ln)
        if m:
            owners[m.group(1)] = None
    return {p: owners.get(p, "?") for p in paths}


def _last_pacman_change() -> float:
    """Newest install or upgrade timestamp. The line 'appeared without any
    install' is drawn from here: newer than every package transaction means
    no package transaction put it there."""
    newest = 0.0
    local = Path("/var/lib/pacman/local")
    if local.exists():
        for entry in local.iterdir():
            try:
                newest = max(newest, entry.stat().st_mtime)
            except OSError:
                continue
    return newest


def _resolve_exec(cmd: str) -> str | None:
    """The program a launcher runs: field codes and arguments stripped, an env
    wrapper's variable assignments skipped, a bare name looked up on PATH."""
    cmd = re.sub(r"%[fFuUdDnNickvm]", "", cmd).strip()
    parts = shlex.split(cmd)
    if parts and parts[0] == "env":
        parts = parts[1:]
    while parts and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", parts[0]):
        parts = parts[1:]
    if not parts:
        return None
    first = parts[0].strip('"\'')
    if first.startswith("/"):
        return first
    return shutil.which(first)


def _dangerous_perms(manifest: dict) -> list[str]:
    reasons = []
    perms = list(manifest.get("permissions") or []) \
        + list(manifest.get("host_permissions") or [])
    content = json.dumps(manifest.get("content_scripts") or [])
    for pattern, why in PERM_REASONS:
        if pattern in perms or pattern in content:
            reasons.append(why)
    return reasons


def check_browser(base: Path = None) -> list[str]:
    """What is loaded into Brave and where it came from, plus the binary and
    any flash plugin checksummed against pacman's records. A malicious browser
    extension is the most common persistence there is: it survives reboots,
    sees every page you are logged into, and never shows on the desktop."""
    base = base or BRAVE_BASE
    findings = []
    if not base.exists():
        return ["info: no Brave profile found on this machine"]
    components, seen = [], 0
    for profile in sorted(base.glob("*/Preferences")):
        try:
            data = json.loads(profile.read_text(errors="replace"))
        except (OSError, ValueError):
            findings.append(f"warn: {profile.parent.name}/Preferences unreadable")
            continue
        settings = data.get("extensions", {}).get("settings", {})
        for ext_id, entry in settings.items():
            if not isinstance(entry, dict):
                continue
            raw = entry.get("path") or ""
            if entry.get("location") == 5:
                # bundled components keep their payload in the root <id>/<ver>
                # dirs below; the Preferences path is often a resource dir with
                # no manifest of its own, so nothing is demanded of it here
                seen += 1
                continue
            seen += 1
            path = Path(raw) if raw.startswith("/") \
                else profile.parent / "Extensions" / ext_id / raw
            manifest_path = path / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(errors="replace"))
            except (OSError, ValueError):
                findings.append(f"warn: extension {ext_id[:12]} has no readable "
                                "manifest where the profile says it lives")
                continue
            name = str(manifest.get("name", ext_id))[:48]
            reasons = _dangerous_perms(manifest)
            try:
                fresh = time.time() - manifest_path.stat().st_mtime < 2 * 86400
            except OSError:
                fresh = False
            if not entry.get("from_webstore"):
                tag = "alert" if entry.get("location") == 4 else "warn"
                findings.append(f"{tag}: {name} was not installed from the "
                                "official store")
            if fresh:
                findings.append(f"warn: {name} was installed in the last two days")
            if reasons:
                findings.append(f"warn: {name} can {', '.join(reasons[:3])}")
    # the component payload lives at the root as <id>/<version>/: whatever sits
    # there must belong to a package, because a planted component appears here
    for manifest_path in list(base.glob("[0-9a-f]" * 32 + "/*/manifest.json"))[:60]:
        try:
            name = str(json.loads(manifest_path.read_text(errors="replace"))
                       .get("name", manifest_path.parent.parent.name))[:48]
        except (OSError, ValueError):
            name = manifest_path.parent.parent.name[:12]
        components.append((str(manifest_path), name))
    if components:
        owners = _pacman_owners([p for p, _ in components])
        for path, name in components:
            pkg = owners.get(path)
            if pkg is None or pkg == "?":
                findings.append(f"alert: browser component {name} is not owned "
                                "by any package")
    if seen == 0:
        findings.append("info: no extensions are loaded in Brave")
    elif not findings:
        findings.append(f"ok: {seen} extension(s) in Brave, all from the store "
                        "or owned by a package, nothing dangerous")
    try:
        out = _pacman_verify("brave-bin")
        altered = [ln for ln in out.splitlines() if "altered file" in ln.lower()
                   and "0 altered" not in ln.lower()]
        if altered:
            findings.append("alert: the brave binary has files that differ from "
                            f"its package: {altered[0][:100]}")
        else:
            findings.append("ok: the brave binary matches the checksums its "
                            "package recorded")
    except _Fail as exc:
        findings.append(f"warn: could not verify the brave binary: {exc}")
    flash_seen = False
    for plugin_dir in (Path("/usr/lib/PepperFlash"), base / "PepperFlash"):
        if not plugin_dir.exists():
            continue
        flash_seen = True
        try:
            pkg = _pacman_owners([str(plugin_dir)]).get(str(plugin_dir))
            if pkg is None:
                findings.append(f"warn: a flash plugin at {plugin_dir} is not "
                                "owned by any package")
            elif pkg != "?":
                out = _pacman_verify(pkg.split()[0])
                ok = "0 altered files" in out.lower()
                findings.append(f"{'ok' if ok else 'alert'}: the flash plugin at "
                                f"{plugin_dir} "
                                + ("matches its package" if ok
                                   else "differs from its package"))
        except _Fail as exc:
            findings.append(f"warn: could not verify the flash plugin: {exc}")
    if not flash_seen:
        findings.append("ok: no flash plugin is installed")
    return findings


def check_launchers(dirs: tuple = None, unit_dir: Path = None) -> list[str]:
    """Desktop entries, autostart files and user services: the places a dropped
    payload makes itself start. Fires on the three shapes malware takes here:
    running from a scratch directory, running a file no package owns, and a
    file that appeared after the last install without one."""
    dirs = dirs or LAUNCHER_DIRS
    unit_dir = unit_dir or SYSTEMD_USER_DIR
    findings, targets, entries = [], [], []
    for label, directory, is_system in dirs:
        if not directory.exists():
            continue
        for entry in sorted(directory.glob("*.desktop")):
            try:
                body = entry.read_text(errors="replace")
            except OSError:
                continue
            in_section = False
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("["):
                    in_section = line == "[Desktop Entry]"
                    continue
                if not in_section or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key not in ("Exec", "TryExec") or not value.strip():
                    continue
                target = _resolve_exec(value)
                if target:
                    entries.append((label, entry.name, target, is_system))
    # user systemd services belong in the same ledger: same ownership question,
    # same autostart role. check_user_units stays the scratch-path specialist.
    if unit_dir.exists():
        for unit in sorted(unit_dir.glob("*.service")):
            try:
                body = unit.read_text(errors="replace")
            except OSError:
                continue
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("Exec") and "=" in line:
                    target = _resolve_exec(line.split("=", 1)[1])
                    if target:
                        entries.append(("user services", unit.name, target, False))
    for _, _, target, _ in entries:
        if target not in targets:
            targets.append(target)
    owners = _pacman_owners(targets) if targets else {}
    last_install = _last_pacman_change()
    for label, entry_name, target, is_system in entries:
        low = target.lower()
        if any(part in low for part in SCRATCH_PARTS):
            findings.append(f"alert: {entry_name} ({label}) runs from a scratch "
                            f"directory: {target}")
            continue
        owned = owners.get(target)
        if owned == "?":
            continue                                   # pacman failed; not evidence
        if owned is None:
            # /usr/local and the user's own tree are where local scripts live by
            # design; flagging old ones trained the report to cry wolf, so they
            # only warn when they appeared after the last package transaction.
            # A file dropped into /usr itself has no such excuse.
            local_ground = target.startswith("/usr/local/") or not is_system
            if not local_ground:
                findings.append(f"alert: {entry_name} ({label}) runs {target}, "
                                "and no package owns it: a system launcher should "
                                "always come from one")
            else:
                try:
                    fresh = last_install > 0 \
                        and Path(target).stat().st_mtime > last_install
                except OSError:
                    fresh = False
                if fresh:
                    findings.append(f"warn: {entry_name} ({label}) runs {target}, "
                                    "which no package owns and which appeared "
                                    "after the last install")
                else:
                    findings.append(f"info: {entry_name} ({label}) runs {target}, "
                                    "yours, no package owns it")
    if not entries:
        findings.append("info: no launchers found to check")
    elif not findings:
        findings.append(f"ok: {len(entries)} launcher target(s) across desktop "
                        "entries, autostart and user services, all accounted for")
    # what nobody owns is what a scanner should meet. The paths are handed to
    # the virus check rather than scanned here, because every clamav invocation
    # reloads 168 MB of signatures (six seconds measured) and two invocations is
    # twice that for no reason. The check runs after this one, and a launcher
    # target is worth more attention than a scratch file, so it goes in front.
    to_scan = [t for t in targets if owners.get(t) is None and Path(t).exists()][:40]
    if to_scan:
        _scan_extra.extend(to_scan)
        findings.append(f"info: {len(to_scan)} unowned launcher target(s) go to the "
                        "virus check below")
    return findings


# Paths that the virus check should scan first, put there by the launcher check.
# Module state rather than a parameter because the checks in CHECKS take no
# arguments, and this is one sweep in one thread by design. A second sweep
# running at the same time would at worst scan a target twice.
_scan_extra: list[str] = []


def _clamav_files(paths: list[str], what: str) -> list[str]:
    """Scan explicit files, under the same cap as the directory sweep."""
    engine = _clamav_engine()
    try:
        proc = subprocess.run([*engine, *paths], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              timeout=config.SWEEP_CLAMAV_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return [f"warn: the scan of {len(paths)} {what}(s) hit the "
                f"{config.SWEEP_CLAMAV_TIMEOUT_S:.0f}s limit partway"]
    except FileNotFoundError:
        return [f"warn: clamscan is not installed, {what}s went unscanned"]
    hits, error, _skipped = _clamav_result(proc)
    if hits:
        return [f"alert: clamav flagged {ln[:120]}" for ln in hits[:8]]
    if error:
        return [f"warn: the scan of {len(paths)} {what}(s) did not run "
                f"({engine[0]}: {error})"]
    return [f"ok: clamav scanned {len(paths)} {what}(s) and found nothing"]


def check_updates() -> list[str]:
    try:
        out = _run(["pacman", "-Qu"], timeout=60)
    except _Fail:
        return ["info: could not count pending updates"]
    n = len([ln for ln in out.splitlines() if ln.strip()])
    return [f"info: {n} package update(s) pending; old packages carry known holes"]


_engine_choice: list[str] | None = None


def _clamav_engine() -> list[str]:
    """Which clamav binary to run, checked by using it rather than asking it.

    clamdscan talks to a running daemon and so skips loading the signature
    database, measured at six seconds for a single file here, because clamscan
    reloads 168 MB of signatures on every invocation. But clamdscan --version
    succeeds with no daemon running: it printed its own version, exited zero, and
    then every scan failed with "could not connect to clamd". Probing by version
    therefore chose a broken engine and the failure read as clean, which is the
    one outcome a security check must never produce. The probe scans a real file.

    Decided once per process: the probe itself costs a database load with
    clamscan, so probing per scan would pay that twice and give back most of
    what the daemon path was for.
    """
    global _engine_choice
    if _engine_choice is not None:
        return _engine_choice
    chosen = ["clamscan", "--no-summary", "--infected"]
    if shutil.which("clamdscan"):
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".probe", delete=False) as fh:
                fh.write("clamd probe\n")
                probe_path = fh.name
            try:
                probe = subprocess.run(["clamdscan", "--no-summary", "--infected",
                                        probe_path], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, timeout=20)
                if probe.returncode == 0:
                    chosen = ["clamdscan", "--no-summary", "--infected", "--fdpass"]
            finally:
                Path(probe_path).unlink(missing_ok=True)
        except (subprocess.TimeoutExpired, OSError):
            pass
    _engine_choice = chosen
    return chosen


def _clamav_result(proc: subprocess.CompletedProcess) -> tuple[list[str], str, int]:
    """(hits, fatal error). clamav exits 0 clean, 1 with findings, 2 on trouble.

    The exit code is the point: a scan that could not run at all must not be
    reported as a scan that found nothing, whatever it printed to stdout. The
    other half is not crying wolf: clamscan also exits 2 for things it merely
    skipped, a socket or an unreadable file, and reporting that as "the scan did
    not run" over a scan that covered four hundred files teaches the reader to
    ignore the line, which is worse than the line never appearing. Warnings are
    returned separately from fatal errors.
    """
    out = proc.stdout.decode(errors="replace")
    hits = [ln.strip() for ln in out.splitlines() if "FOUND" in ln]
    if hits:
        return hits, "", 0
    if proc.returncode and proc.returncode != 1:
        lines = [ln.strip() for ln in proc.stderr.decode(errors="replace").splitlines()
                 if ln.strip()]
        fatal = [ln for ln in lines if not ln.startswith("WARNING")]
        if fatal:
            return [], "; ".join(ln[:120] for ln in fatal[-2:]), 0
        # nothing fatal: the scan ran, and some files were skipped
        skipped = len([ln for ln in lines if ln.startswith("WARNING")])
        return [], "", skipped
    return [], "", 0


def _newest_files(dirs: list[str], limit: int) -> tuple[list[str], int, int]:
    """The newest files under these directories, with the totals.

    Newest first, because a fresh dropper is a recent file, and bounded, because
    a scratch directory is mostly build litter: this machine's /tmp held 9,859
    files and 1.2 GB, which is two minutes of scanning that never finished.
    Returns (files, seen, bytes of the chosen files).
    """
    found: list[tuple[float, str]] = []
    seen = 0
    for root in dirs:
        for base, _dirs, names in os.walk(root, onerror=lambda e: None):
            for name in names:
                seen += 1
                if seen > config.SWEEP_CLAMAV_ENUMERATE:
                    break
                path = os.path.join(base, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue        # a socket or fifo is not a file to scan, and
                                    # clamscan exits 2 on one, which used to make
                                    # the whole scan look like it had failed
                found.append((info.st_mtime, path))
            if seen > config.SWEEP_CLAMAV_ENUMERATE:
                break
    found.sort(reverse=True)
    chosen = [path for _when, path in found[:limit]]
    total = 0
    for path in chosen:
        try:
            total += os.lstat(path).st_size
        except OSError:
            pass
    return chosen, seen, total


def check_clamav() -> list[str]:
    findings = []
    # Taken once, at the top, because there are four ways out of this check early
    # and three of them never reached the line that used to tidy up. The launcher
    # check hands over its unowned targets on every sweep, so a machine that runs
    # for a day accumulated them up to the cap, and a scan that never happened
    # still looked like it had the whole list in front of it.
    preferred = [p for p in _scan_extra if os.path.isfile(p)]
    _scan_extra.clear()
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
    if not existing:
        return findings + ["warn: none of the usual staging directories exist"]

    files, seen, chosen_bytes = _newest_files(existing, config.SWEEP_CLAMAV_MAX_FILES)
    # the launcher targets first: they are the files nothing accounts for
    if preferred:
        files = preferred + [f for f in files if f not in preferred]
        files = files[:config.SWEEP_CLAMAV_MAX_FILES]
    if not files:
        findings.append(f"ok: nothing to scan under {', '.join(existing)}")
        return findings

    engine = _clamav_engine()
    argv = [*engine, "--max-filesize=100M", "--max-scansize=400M", *files]
    started = time.time()
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=config.SWEEP_CLAMAV_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return findings + [
            f"warn: the scan was cut off after "
            f"{config.SWEEP_CLAMAV_TIMEOUT_S:.0f}s partway through "
            f"{len(files)} file(s) under {', '.join(existing)}: partial coverage"]
    except FileNotFoundError:
        return findings + ["warn: clamscan is not installed"]

    took = time.time() - started
    hits, error, skipped = _clamav_result(proc)
    if hits:
        findings += [f"alert: clamav flagged {ln[:120]}" for ln in hits[:8]]
        return findings
    if error:
        # an unscannable directory is not a clean directory
        return findings + [f"warn: the scan of {', '.join(existing)} did not run "
                           f"({engine[0]}: {error})"]
    # coverage is stated, not implied: "found nothing" over an unbounded pile of
    # files is not the same claim as "found nothing in the newest four hundred"
    covered = (f"the newest {len(files)} of {seen} file(s)" if seen > len(files)
               else f"all {seen} file(s)")
    if preferred:
        findings.append(f"ok: clamav checked the {len(preferred)} unowned launcher "
                        f"target(s) and {covered} under {', '.join(existing)} "
                        f"({chosen_bytes / 1e6:.0f} MB) in {took:.0f}s and found nothing"
                        + (f", skipping {skipped} it could not read" if skipped else ""))
    else:
        findings.append(f"ok: clamav checked {covered} under {', '.join(existing)} "
                        f"({chosen_bytes / 1e6:.0f} MB) in {took:.0f}s and found nothing"
                        + (f", skipping {skipped} it could not read" if skipped else ""))
    if seen > len(files):
        findings.append(f"info: {seen - len(files)} older file(s) were not checked, "
                        f"by choice: the cap is {config.SWEEP_CLAMAV_MAX_FILES}")
    return findings

# -------------------------------------------------------------- macOS checks
# A Mac has no /proc, no systemd and no package manager to check against, so the
# same questions are asked of the switches it does have. launchd is systemd's
# counterpart and its plist is the unit file; the code signature is the package
# manager's checksum by another name; Gatekeeper is the thing that is supposed to
# stop an unsigned download from running in the first place.
LAUNCH_DIRS = (
    ("user agents", HOME / "Library/LaunchAgents"),
    ("system agents", Path("/Library/LaunchAgents")),
    ("system daemons", Path("/Library/LaunchDaemons")),
)
MAC_SCRATCH = ("/tmp/", "/var/tmp/", "/private/tmp/", "/private/var/tmp/",
               "/users/shared/", "/downloads/", "/.cache/")
# Mach-O and fat binaries, in both byte orders. Only these are worth asking
# codesign about: a launch item that runs a shell script has no signature to
# verify, and is judged on where it lives and who can write to it instead.
MACHO_MAGIC = (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe",
               b"\xfe\xed\xfa\xce", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")
CODESIGN_LIMIT = 20


def _plist_target(body) -> str | None:
    """The program a launch item runs: Program, or the first of ProgramArguments."""
    if not isinstance(body, dict):
        return None
    program = body.get("Program")
    if isinstance(program, str) and program.strip():
        return program.strip()
    args = body.get("ProgramArguments")
    if isinstance(args, list):
        for arg in args:
            if isinstance(arg, str) and arg.strip():
                return arg.strip()
    return None


def _is_macho(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) in MACHO_MAGIC
    except OSError:
        return False


def _codesign_complaint(path: str, cache: dict) -> str:
    """What codesign says is wrong, or an empty string when it verifies.

    Cached per path, because a launch item and the daemon it starts are often the
    same binary and verifying one twice is a second of nothing. A machine with no
    codesign is not evidence of anything, so it produces no finding.
    """
    if path not in cache:
        cache[path] = ""
        try:
            proc = subprocess.run(["codesign", "-v", "--verify", path],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=30)
            if proc.returncode != 0:
                said = (proc.stderr or proc.stdout).decode(errors="replace")
                lines = [ln.strip() for ln in said.splitlines() if ln.strip()]
                cache[path] = lines[-1][:80] if lines else "no valid signature"
        except (OSError, subprocess.SubprocessError):
            cache[path] = ""
    return cache[path]


def check_mac_launch_items(dirs: tuple = None) -> list[str]:
    """What starts at login, and whether the program it starts can be trusted.

    Three questions per item, in the order that matters: is the plist itself
    writable by anyone (then the answer to what runs is whoever got there
    first), does the program it names live in a scratch directory, and is the
    program signed. The third is the package manager's checksum by another name,
    and it is only asked of an actual binary.
    """
    findings: list[str] = []
    cache: dict[str, str] = {}
    candidates = checked = 0
    for _label, base in (dirs or LAUNCH_DIRS):
        if not base.exists():
            continue
        for item in sorted(base.glob("*.plist")):
            try:
                mode = item.stat().st_mode
                with item.open("rb") as fh:
                    body = plistlib.load(fh)
            except OSError as exc:
                findings.append(f"warn: {item.name} could not be read: {exc}")
                continue
            except Exception as exc:               # present, but not a plist
                findings.append(f"warn: {item.name} is not a readable plist: {exc}")
                continue
            if mode & 0o002:
                findings.append(f"alert: {item} is world-writable, so anyone can "
                                f"change what runs at login")
            target = _plist_target(body)
            if not target:
                continue
            if any(part in target.lower() for part in MAC_SCRATCH):
                findings.append(f"alert: {item.name} launches from a scratch "
                                f"directory: {target[:70]}")
                continue
            program = Path(target)
            if not program.exists():
                findings.append(f"warn: {item.name} runs {target[:70]}, which is "
                                f"not there")
                continue
            try:
                if program.stat().st_mode & 0o002:
                    findings.append(f"alert: {item.name} runs a program anyone can "
                                    f"write to: {target[:70]}")
                    continue
            except OSError:
                pass
            if _is_macho(program):
                candidates += 1
                if checked < CODESIGN_LIMIT:
                    checked += 1
                    if complaint := _codesign_complaint(target, cache):
                        findings.append(f"warn: {item.name} runs an unsigned or "
                                        f"broken binary: {target[:60]} ({complaint})")
    if not findings:
        findings.append("ok: no launch item runs from a scratch directory, an "
                        "unsigned binary or a file anyone can write to")
    if candidates > checked:
        findings.append(f"info: {candidates - checked} more signature(s) were not "
                        f"checked, by choice: the cap is {CODESIGN_LIMIT}")
    return findings


def check_mac_extensions() -> list[str]:
    """Third-party system extensions: the closest thing here to a kernel module.

    Read rather than trusted. If the tool's output has moved and the rows no
    longer match, the count in the tool's own header says so, and that is
    reported instead of a clean bill of health for a list nobody actually read.
    """
    try:
        out = _run(["systemextensionsctl", "list"], timeout=20)
    except _Fail as exc:
        return [f"warn: could not list system extensions: {exc}"]
    said = re.search(r"(\d+)\s+extension", out)
    listed = int(said.group(1)) if said else None
    third = []
    for line in out.splitlines():
        row = re.match(r"^\s*[*+-]\s+[*+-]\s+(\S+)\s+(\S+)\s+\(", line)
        if row and not row.group(2).startswith("com.apple."):
            third.append(row.group(2))
    if third:
        return [f"info: third-party system extensions are installed: "
                f"{', '.join(sorted(set(third))[:6])}: confirm you know each one"]
    if listed:
        return [f"warn: {listed} system extension(s) are installed but the list "
                f"could not be read, so none of them were judged"]
    return ["ok: no third-party system extensions are installed"]


def check_mac_gatekeeper() -> list[str]:
    try:
        said = _run(["spctl", "--status"], timeout=20).strip().lower()
    except _Fail as exc:
        return [f"warn: could not ask Gatekeeper anything: {exc}"]
    if "enabled" in said:
        return ["ok: Gatekeeper is on, so an unsigned app is stopped before it runs"]
    if "disabled" in said:
        return ["alert: Gatekeeper is off: unsigned apps run with no check at all"]
    return [f"warn: Gatekeeper answered oddly: {said[:60]}"]


def check_mac_processes() -> list[str]:
    """The dropped-payload check, without /proc to read it from.

    ps prints the path of each executable, which is what makes the question
    askable here at all. A Mac has no /proc/<pid>/exe and no deleted-file marker
    to look for, so this one is only about where a running program lives.
    """
    try:
        out = _run(["ps", "-axo", "pid=,comm="])
    except _Fail as exc:
        return [f"warn: could not list processes: {exc}"]
    findings = []
    for line in out.splitlines():
        pid, _, path = line.strip().partition(" ")
        path = path.strip()
        if path and any(part in path.lower() for part in MAC_SCRATCH):
            findings.append(f"alert: pid {pid} runs from {path[:70]}")
    return findings or ["ok: no process runs from a scratch directory"]


def check_mac_listeners() -> list[str]:
    """What is listening, with lsof because a Mac has no ss.

    -F asks for field output rather than the columnar form, which is the one
    part of lsof's output that does not shift when a command name happens to
    contain a space.
    """
    try:
        out = _run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fcn"], timeout=20)
    except _Fail:
        out = ""
    listening: list[str] = []
    command = ""
    for line in out.splitlines():
        if line.startswith("c"):
            command = line[1:].strip()
        elif line.startswith("n"):
            listening.append(f"{command or 'a process'} on {line[1:].strip()}")
    if not listening:                              # lsof is shipped, but not always
        try:
            out = _run(["netstat", "-an", "-p", "tcp"], timeout=20)
        except _Fail as exc:
            return [f"warn: could not list listeners: {exc}"]
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[-1] == "LISTEN":
                # netstat separates the host from the port with a dot where lsof
                # uses a colon, so the two shapes are made one before the filters
                # below see them. rpartition, because an IPv6 host has dots in it
                # and the port is what comes last.
                host, _, port = parts[3].rpartition(".")
                listening.append(f"a process on {host}:{port}" if host
                                 else f"a process on {parts[3]}")
    discovery = re.compile(r":(5353|5355|546)(%\w+)?$")
    odd = {item for item in listening
           if not re.search(r"(127\.0\.0\.1|\[::1\]):", item)
           and not discovery.search(item)}
    if not odd:
        return ["ok: nothing listening beyond loopback and discovery services"]
    return [f"warn: listening beyond loopback: {'; '.join(sorted(odd)[:8])}"]


# --------------------------------------------------------- the deeper checks
# Asked for rather than run every time: lynis takes about thirty seconds on this
# machine and rkhunter minutes. Both are read as findings, not as walls of text,
# and both are honest about what they could not reach.
_LYNIS_INSTALL = "install lynis (pacman -S lynis) and ask again"
_RKHUNTER_INSTALL = "install rkhunter (pacman -S rkhunter) and ask again"


_TOOL_PATHS = ("/usr/bin", "/usr/sbin", "/usr/local/bin", "/usr/local/sbin", "/bin", "/sbin")


def _find(tool: str) -> str | None:
    """Where the tool is, runnable or not.

    shutil.which only finds what this user could run, and rkhunter ships as 0700
    root on Arch: which() said it was missing, which was a lie, and the report
    then told the user to install something they already had.
    """
    found = shutil.which(tool)
    if found:
        return found
    for directory in _TOOL_PATHS:
        candidate = os.path.join(directory, tool)
        if os.path.exists(candidate):
            return candidate
    return None


def _rkhunter_lines(output: str) -> list[str]:
    """rkhunter's report, read down to what a person needs.

    Measured on this machine as root: 40 seconds and 76 warnings, most of them the
    same one about a file whose properties have changed, once per upgraded binary.
    Forty lines of that in a spoken summary is noise, so they are counted and
    named, and the handful of other warnings are quoted. Its own subshell noise
    (egrep: warning: egrep is obsolescent) is not a warning about this machine.
    """
    warnings, files = [], []
    for line in output.splitlines():
        text = line.strip()
        if text.lower().startswith("warning:"):
            body = text.split(":", 1)[1].strip()
            if body:
                warnings.append(body[:160])
        elif text.lower().startswith("file:") and warnings:
            files.append(text.split(":", 1)[1].strip()[:120])
    found = []
    if files:
        found.append(f"warn: rkhunter: {len(files)} file(s) no longer match its "
                     f"record, which is what a package upgrade looks like: "
                     + "; ".join(files[:3]))
    kind = [w for w in warnings if "properties have changed" not in w.lower()]
    if kind:
        found.append(f"warn: rkhunter raised {len(kind)} other warning(s)")
        found += [f"warn: rkhunter: {w}" for w in kind[:6]]
    if not found:
        found.append("ok: rkhunter found no warnings on this run")
    return found


def _lynis_lines(output: str) -> list[str]:
    """lynis's audit, read down to the index and the first few things it suggests.

    Its report counts its own findings, so those numbers are used rather than the
    lines that were scraped, and the notices it prints about running without root
    are not findings about this machine.
    """
    index = tests = None
    version = ""
    warnings: list[str] = []
    suggestions: list[str] = []
    declared = {"warn": None, "suggest": None}
    section = None
    for line in output.splitlines():
        text = line.strip()
        if text.startswith("Lynis") and not version:
            version = text[:40]
        if "Hardening index" in text:
            index = text.split(":", 1)[1].strip().split()[0]
        elif "Tests performed" in text:
            tests = text.split(":", 1)[1].strip().split()[0]
        elif text.startswith("Warnings (") or text.startswith("Suggestions ("):
            section = "warn" if text.startswith("Warnings") else "suggest"
            digits = "".join(ch for ch in text if ch.isdigit())
            declared[section] = int(digits) if digits else None
        elif line.startswith("  * ") and not line.startswith("      ") and section:
            body = text[2:].strip()
            if re.search(r"require root permissions|fail silently", body, re.I):
                continue                          # a notice about the audit itself
            tag = body[body.find("["):body.find("]") + 1] if "[" in body else ""
            entry = body.split("[")[0].strip()[:130] + (" " + tag if tag else "")
            (warnings if section == "warn" else suggestions).append(entry)
    found = []
    if index:
        found.append(f"ok: lynis audit: hardening index {index}/100, "
                     f"{tests or '?'} tests performed")
    else:
        found.append("warn: lynis ran but its report could not be read")
    counted = declared["warn"] if declared["warn"] is not None else len(warnings)
    if counted:
        found.append(f"warn: lynis raised {counted} warning(s): "
                     + "; ".join(warnings[:3]))
    else:
        found.append("ok: lynis raised no warnings")
    counted = declared["suggest"] if declared["suggest"] is not None else len(suggestions)
    if counted:
        found.append(f"info: lynis suggested {counted} change(s): "
                     + "; ".join(suggestions[:4]))
    if "require root permissions" in output:
        found.append("info: lynis ran as your user, so the tests needing root "
                     "were skipped")
    return found


def _lynis_profile() -> Path | None:
    """A profile that leaves out the test that asks the user for their password.

    NETW-2706 runs `resolvectl statistics`, and on a machine where that action is not
    allowed for the user polkit does not fail quietly: it puts an authentication
    dialog on the desktop, mid-sweep, every single time. A security check that asks
    for your password is not one you keep running. The free lynis has no flag for
    skipping a test, so it is a profile file, and the reason is written in it for
    whoever reads it next.
    """
    if not config.SWEEP_LYNIS_SKIP_TESTS:
        return None
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = config.STATE_DIR / "lynis.prf"
        body = ["# Written by ARCHY. The tests below are left out because they ask for",
                "# authentication, which puts a password dialog on the desktop."]
        body += [f"skip-test={name}" for name in config.SWEEP_LYNIS_SKIP_TESTS]
        path.write_text("\n".join(body) + "\n")
        return path
    except OSError as exc:
        print(f"[sweep] no lynis profile: {exc}")
        return None


def check_lynis() -> list[str]:
    tool = _find("lynis")
    if not tool:
        return [f"info: lynis is not installed, so the deeper audit was skipped: "
                f"{_LYNIS_INSTALL}"]
    argv = [tool, "audit", "system", "--quick", "--no-colors"]
    profile = _lynis_profile()
    if profile:
        argv += ["--profile", str(profile)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=config.SWEEP_LYNIS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"warn: lynis could not be run here: {type(exc).__name__}"]
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode not in (0, 1) or not output.strip():
        return [f"warn: lynis exited {proc.returncode} without a report"]
    lines = _lynis_lines(output)
    if "timed out" in output.lower() or "killed" in output.lower():
        lines.append(f"warn: the audit was cut off at "
                     f"{config.SWEEP_LYNIS_TIMEOUT_S:.0f}s, so it covered part of "
                     "the machine")
    return lines


def check_rkhunter() -> list[str]:
    tool = _find("rkhunter")
    if not tool:
        return [f"info: rkhunter is not installed, so the rootkit check was skipped: "
                f"{_RKHUNTER_INSTALL}"]
    if not os.access(tool, os.X_OK):
        # measured on this machine: /usr/bin/rkhunter is 0700 root, so it cannot
        # be run by anyone else, and its log is 0600 for the same reason. Saying
        # "no rootkits" from a check that never ran is the one answer worse than
        # saying nothing, so this says exactly what happened and what to run.
        return ["warn: rkhunter is installed but only root can run it, so the "
                "rootkit check did not run: run sudo rkhunter --check --sk yourself",
                "info: rkhunter's log is root only too, so it cannot be read from here"]
    try:
        proc = subprocess.run([tool, "--check", "--sk", "--nocolors",
                               "--report-warnings-only"],
                              capture_output=True, text=True,
                              timeout=config.SWEEP_RKHUNTER_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"warn: rkhunter could not be run here: {type(exc).__name__}"]
    output = (proc.stdout or "") + (proc.stderr or "")
    if not output.strip():
        return ["warn: rkhunter exited without a report"]
    lines = _rkhunter_lines(output)
    if proc.returncode != 0 and "warning:" not in output.lower():
        lines.append(f"warn: rkhunter exited {proc.returncode} without saying what "
                     "it found")
    return lines


def deep_checks() -> tuple:
    """The checks that only make sense when asked for, on the machine they fit."""
    return DEEP_CHECKS if sys.platform.startswith("linux") else ()


def deep_wanted(text: str) -> bool:
    words = text.lower()
    return any(w in words for w in config.SWEEP_DEEP_WORDS)


DEEP_CHECKS = (check_rkhunter, check_lynis)

CHECKS = (check_processes, check_uid_zero, check_hosts, check_world_writable,
          check_listeners, check_ssh, check_user_units, check_browser,
          check_launchers, check_updates, check_clamav)
MAC_CHECKS = (check_mac_processes, check_hosts, check_mac_listeners,
              check_mac_launch_items, check_mac_extensions, check_mac_gatekeeper)


def wanted(text: str) -> bool:
    """Does this request ask for a sweep? Shares the trigger words with the
    assistant, which calls the checks and hands the report to the model."""
    words = text.lower()
    return any(w in words for w in config.SWEEP_WORDS)


def supported() -> bool:
    """Is this a machine the checks know how to read?

    Linux is read through /proc, systemd and the package manager; macOS through
    launchd, the code signature and Gatekeeper. They are different questions with
    different answers, but both are the same sweep from the outside.

    Windows has neither, and a report of nothing but "unavailable" would be worse
    than saying so, which is what happens instead.
    """
    return sys.platform.startswith("linux") or sys.platform == "darwin"


def checks() -> tuple:
    """The checks that know how to read this machine."""
    return MAC_CHECKS if sys.platform == "darwin" else CHECKS


def run(deep: bool = False) -> str:
    """Run every check and return a compact plain-text report for the model.

    The deeper checks are added only when asked for, because they are minutes
    rather than seconds, and the ordinary sweep is meant to be one breath.
    """
    if not supported():
        return ("security sweep: this machine is neither Linux nor macOS, so "
                "there is nothing here these checks know how to read.")
    # platform.node() rather than os.uname(), which Windows does not have at all:
    # supported() keeps this line out of reach there today, and a line that throws on
    # a platform is a trap for whoever makes that platform work tomorrow
    lines = [f"security sweep of {platform.node()}, {time.strftime('%H:%M')}"
             + (", with the deeper checks:" if deep else ":")]
    for check in checks() + (deep_checks() if deep else ()):
        try:
            lines += [f"  {f}" for f in check()]
        except Exception as exc:                      # a check must never sink the sweep
            lines.append(f"  warn: {check.__name__} failed: {exc}")
    alerts = sum(1 for ln in lines if "alert:" in ln)
    warns = sum(1 for ln in lines if ln.strip().startswith("warn:"))
    verdict = "nothing needs action" if not alerts else f"{alerts} thing(s) need action"
    lines.append(f"  summary: {verdict}, {warns} warning(s) worth reading")
    report = "\n".join(lines)
    # kept on disk so the briefing can say what the last sweep found without
    # running another one
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (config.STATE_DIR / "last-sweep.txt").write_text(report)
    except OSError:
        pass
    return report if len(report) < 3800 else report[:3700] + "\n  (report truncated)"
