"""Reading the machine: cpu, memory, uptime, load, temperature.

One small surface over three platforms. `hud.py` wants a cpu percentage and a
memory reading for the dashboard dials; `briefing.py` wants the same numbers as
a sentence. Both read `/proc` directly, which is the one reason those two
features were Linux-only: a machine without `/proc` has no readings at all, and
a dashboard that quietly shows zero is worse than one that admits it does not
know.

Linux reads `/proc` and `/sys`. macOS asks the mach kernel for cpu ticks, and
`vm_stat` for memory rather than the same mach call, because the virtual memory
call has changed shape across releases while the tool that prints it has not.
Windows still reports nothing, which is what it did before this existed.

Nothing here raises. Every function answers or says it does not know, because
each caller is either a thread that must not die or a sentence that must read
as a sentence.
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

GIB = 1024.0 ** 3

IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"


# ---------------------------------------------------------------------- linux
def _linux_cpu_ticks() -> tuple[int, int] | None:
    with open("/proc/stat") as fh:
        parts = [int(x) for x in fh.readline().split()[1:]]
    idle = parts[3] + parts[4]                    # idle + iowait
    return idle, sum(parts)


def _linux_memory() -> tuple[float, float]:
    info: dict[str, float] = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            info[key] = float(rest.strip().split()[0])
    total, avail = info.get("MemTotal", 1), info.get("MemAvailable", 0)
    return 100.0 * (1 - avail / total), (total - avail) / 1024 / 1024


def _linux_uptime() -> float | None:
    with open("/proc/uptime") as fh:
        return float(fh.read().split()[0])


def _linux_temperature() -> float | None:
    """The package temperature, from whichever sensor names one.

    Only the cpu packages are read. A machine reports a dozen sensors between
    hwmon directories, most of them a disk or a wifi card, and the interesting
    number is the one labelled package.
    """
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            if (hwmon / "name").read_text().strip() not in ("coretemp", "k10temp", "zenpower"):
                continue
            for label in sorted(hwmon.glob("temp*_label")):
                if "package" in label.read_text().lower():
                    source = hwmon / label.name.replace("_label", "_input")
                    return int(source.read_text()) / 1000
        except (OSError, ValueError):
            continue
    return None


# ----------------------------------------------------------------------- mac
# The mach calls live in the C library, so they are reached through ctypes.
# Bound once: the argtypes matter more than usual here, because a wrong one
# still returns something plausible rather than failing.
_LIBSYSTEM = None
_HOST_CPU_LOAD_INFO = 3
_CPU_STATE_IDLE = 2
_HW_MEMSIZE: int | None = None
_PAGE_SIZE: int | None = None


class _HostCpuLoadInfo(ctypes.Structure):
    # user, system, idle, nice, in ticks since boot
    _fields_ = [("cpu_ticks", ctypes.c_uint * 4)]


def _libsystem():
    global _LIBSYSTEM
    if _LIBSYSTEM is None:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        lib.mach_host_self.restype = ctypes.c_uint
        lib.mach_task_self.restype = ctypes.c_uint
        lib.mach_port_deallocate.argtypes = [ctypes.c_uint, ctypes.c_uint]
        lib.host_statistics.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_uint)]
        lib.host_statistics.restype = ctypes.c_int
        _LIBSYSTEM = lib
    return _LIBSYSTEM


def _mac_cpu_ticks() -> tuple[int, int] | None:
    """Cpu ticks since boot, the same shape as Linux's first /proc/stat line.

    mach_host_self() hands back a send right on every call, and a program that
    loops on this without giving it back leaks one port reference per poll until
    the port namespace runs out. The assistant polls every 1.5 seconds for hours,
    so the deallocate is not decoration.
    """
    lib = _libsystem()
    info = _HostCpuLoadInfo()
    count = ctypes.c_uint(4)
    host = lib.mach_host_self()
    try:
        rc = lib.host_statistics(host, _HOST_CPU_LOAD_INFO,
                                 ctypes.byref(info), ctypes.byref(count))
    finally:
        lib.mach_port_deallocate(lib.mach_task_self(), host)
    if rc != 0:                                    # KERN_SUCCESS
        return None
    ticks = list(info.cpu_ticks)
    return ticks[_CPU_STATE_IDLE], sum(ticks)


def _mac_total_memory() -> int:
    """Total physical memory, in bytes. It cannot change, so it is read once."""
    global _HW_MEMSIZE
    if _HW_MEMSIZE is None:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        _HW_MEMSIZE = int(out)
    return _HW_MEMSIZE


def _mac_memory() -> tuple[float, float]:
    """Used memory, from `vm_stat`.

    The mach equivalent, host_statistics64, is the tidier call and is what
    psutil uses, but its struct has grown a field at a time and a ctypes mirror
    of it that is subtly wrong returns numbers that look right. `vm_stat` prints
    the same counters under names, so the layout question does not arise.

    Reclaimable pages count as available, the same meaning as Linux's
    MemAvailable: free, inactive and speculative are all memory the system would
    hand over on demand. This is the figure psutil reports as percent, and the
    one thing it does not do is agree with Activity Monitor, which counts most of
    that reclaimable cache as memory in use. Available is the useful number for
    "am I running out", which is the question a briefing is answering.
    """
    out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    match = re.search(r"page size of (\d+) bytes", out)
    if not match:
        raise ValueError("vm_stat did not say its page size")
    page = int(match.group(1))
    pages: dict[str, int] = {}
    for line in out.splitlines():
        found = re.match(r"([A-Za-z][^:]*):\s+(\d+)\.", line)
        if found:
            pages[found.group(1).strip()] = int(found.group(2))
    if "Pages free" not in pages:
        raise ValueError("vm_stat did not list its free pages")
    free = pages.get("Pages free", 0)
    available = free + pages.get("Pages inactive", 0) + pages.get("Pages speculative", 0)
    total = _mac_total_memory()
    if total <= 0:
        raise ValueError("the machine reported no memory at all")
    # Clamped, because the counters do not have to add up: inactive and
    # speculative pages are counted as reclaimable while a machine that has been
    # up for weeks can hold more of them than the total suggests, and an
    # unclamped subtraction there reports negative memory in use.
    used = min(max(total - available * page, 0), total)
    return 100.0 * used / total, used / GIB


def _mac_uptime() -> float | None:
    out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                         text=True, timeout=5).stdout
    match = re.search(r"sec\s*=\s*(\d+)", out)
    if not match:
        return None
    return max(0.0, time.time() - int(match.group(1)))


def _mac_temperature() -> float | None:
    """The cpu temperature, if this machine will say without being asked twice.

    Intel Macs answer through the SMC, which osx-cpu-temp reads. Apple Silicon
    has no unprivileged sensor at all: powermetrics wants root, and the thermal
    level sysctl is a level from 0 to 100 rather than degrees, so it is not a
    temperature and is not reported as one.
    """
    tool = shutil.which("osx-cpu-temp")
    if not tool:
        return None
    try:
        out = subprocess.run([tool], capture_output=True, text=True, timeout=5).stdout
        found = re.search(r"(-?\d+(?:\.\d+)?)", out)
        return float(found.group(1)) if found else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# ------------------------------------------------------------------- surface
def cpu_percent(prev: tuple[int, int]) -> tuple[float, tuple[int, int]]:
    """Cpu load since the previous call, and the state to pass back next time.

    A delta rather than an instant reading, which is why the caller carries the
    state. The same code does Linux and macOS because both hand back ticks since
    boot in the same shape.

    The mac counters are 32-bit and do eventually wrap, and a machine with no
    readings at all lands here too: both answer with the previous state and no
    claim about the load, rather than reporting a spike that is not happening.
    """
    ticks: tuple[int, int] | None = None
    try:
        if IS_LINUX:
            ticks = _linux_cpu_ticks()
        elif IS_MAC:
            ticks = _mac_cpu_ticks()
    except (OSError, ValueError, ctypes.ArgumentError):
        ticks = None
    if ticks is None:
        return 0.0, prev
    idle, total = ticks
    d_idle, d_total = idle - prev[0], total - prev[1]
    if d_total <= 0:
        return 0.0, (idle, total)
    return 100.0 * (1 - d_idle / d_total), (idle, total)


def memory() -> tuple[float, float]:
    """Memory in use: a percentage, and the same figure in gigabytes."""
    try:
        if IS_LINUX:
            return _linux_memory()
        if IS_MAC:
            return _mac_memory()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return 0.0, 0.0


def uptime_seconds() -> float | None:
    try:
        if IS_LINUX:
            return _linux_uptime()
        if IS_MAC:
            return _mac_uptime()
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def load_average() -> tuple[float, float, float] | None:
    """The 1, 5 and 15 minute load averages, or None where there are none.

    os.getloadavg is asked rather than /proc/loadavg read, because it is the
    same file on Linux and the same numbers on macOS and Windows simply has no
    such idea.
    """
    try:
        return os.getloadavg()
    except (OSError, AttributeError):
        return None


def temperature_c() -> float | None:
    try:
        if IS_LINUX:
            return _linux_temperature()
        if IS_MAC:
            return _mac_temperature()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None
