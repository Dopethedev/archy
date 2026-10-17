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
- the browser: what is loaded into Brave, where it came from, and what it may
  see; plus the binary and any flash plugin verified against pacman's records.
- launchers: desktop entries, autostart files and user services, checked for
  scratch directories, missing package ownership, and files that appeared
  without any install; suspicious targets go through clamav as well.

Every check reports, even when clean. Silence is how a skipped check hides.
"""
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
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
    # what nobody owns is what a scanner should meet, bounded as everywhere else
    to_scan = [t for t in targets if owners.get(t) is None and Path(t).exists()]
    if to_scan:
        findings += _clamav_files(to_scan[:40], "launcher target")
    return findings


def _clamav_files(paths: list[str], what: str) -> list[str]:
    """Scan explicit files, under the same cap as the directory sweep."""
    try:
        proc = subprocess.run(["clamscan", "--no-summary", "--infected", *paths],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=config.SWEEP_CLAMAV_TIMEOUT_S)
        out = proc.stdout.decode(errors="replace")
    except subprocess.TimeoutExpired:
        return [f"warn: the scan of {len(paths)} {what}(s) hit the "
                f"{config.SWEEP_CLAMAV_TIMEOUT_S:.0f}s limit partway"]
    except FileNotFoundError:
        return [f"warn: clamscan is not installed, {what}s went unscanned"]
    hits = [ln for ln in out.splitlines() if "FOUND" in ln]
    if hits:
        return [f"alert: clamav flagged {ln.strip()[:120]}" for ln in hits[:8]]
    return [f"ok: clamav scanned {len(paths)} {what}(s) and found nothing"]


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
          check_listeners, check_ssh, check_user_units, check_browser,
          check_launchers, check_updates, check_clamav)


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
    # kept on disk so the briefing can say what the last sweep found without
    # running another one
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (config.STATE_DIR / "last-sweep.txt").write_text(report)
    except OSError:
        pass
    return report if len(report) < 3800 else report[:3700] + "\n  (report truncated)"
