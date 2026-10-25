"""The local model's server, when there is no internet to fall back on.

The offline brain is llama.cpp's router, which pi already knows how to talk to
(docs/llama-cpp.md in the pi package). What it needs from here is small: is it up,
start it if it is not, and stop it only if we were the ones who started it. A
router someone started themselves is theirs to keep.

Nothing here downloads anything. Fetching a model is an explicit setup step, and
several gigabytes appearing during start-up is not a thing a voice assistant
should do on its own.
"""

from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config

_proc: subprocess.Popen | None = None      # only set when we started it


def models_present() -> list[Path]:
    """Every loadable GGUF in the model directory, subdirectories included.

    The vision projector is not a model: it sits beside one as `mmproj-*.gguf`
    and cannot be served on its own, so counting it would report a model on a
    machine that has only downloaded a projector.
    """
    root = Path(config.PI_LOCAL_MODELS_DIR)
    if not root.exists():
        return []
    def usable(paths):
        return sorted(p for p in paths
                      if p.suffix == ".gguf" and not p.name.startswith("mmproj"))
    found = usable(root.glob("*"))
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        found.extend(usable(sub.glob("*")))
    return found


def healthy(timeout: float = 1.5) -> bool:
    """Is a router answering on the configured port, right now?"""
    try:
        with urllib.request.urlopen(f"{config.PI_LOCAL_SERVER_URL}/health",
                                    timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ensure_running(wait: float = 40.0) -> bool:
    """Start the router if it is not up, and say whether it is usable.

    A router someone else started is left alone. Ours is waited for, because the
    first question asked while it is still loading would otherwise be reported as
    a dead model, and the wait is bounded so a router that never comes up does not
    hold the turn open forever.
    """
    global _proc
    if not config.PI_MODELS_OFFLINE:
        return False                        # nothing configured to run
    if healthy():
        return True
    if _proc is not None and _proc.poll() is None:
        return _wait(wait)                  # already starting
    if not Path(config.PI_LOCAL_MODELS_DIR).exists():
        print(f"[local] no model directory: {config.PI_LOCAL_MODELS_DIR}", flush=True)
        return False
    args = [config.PI_LOCAL_SERVER_BIN,
            "--models-dir", str(config.PI_LOCAL_MODELS_DIR),
            "--host", config.PI_LOCAL_SERVER_URL.rsplit(":", 1)[0].removeprefix("http://"),
            "--port", config.PI_LOCAL_SERVER_URL.rsplit(":", 1)[-1],
            *config.PI_LOCAL_SERVER_ARGS]
    if config.PI_LOCAL_DEVICE:
        args += ["--device", config.PI_LOCAL_DEVICE]
    print(f"[local] starting {' '.join(args)}", flush=True)
    try:
        _proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, text=True)
    except (OSError, ValueError) as exc:
        print(f"[local] could not start {config.PI_LOCAL_SERVER_BIN}: {exc}", flush=True)
        return False
    return _wait(wait)


def _wait(wait: float) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if healthy():
            print("[local] the local model server is up", flush=True)
            return True
        if _proc is not None and _proc.poll() is not None:
            print(f"[local] it exited with {_proc.returncode}", flush=True)
            return False
        time.sleep(0.4)
    print(f"[local] it did not come up within {wait:.0f}s", flush=True)
    return False


def stop() -> None:
    """Stop the router, but only one of ours. Never touches someone else's."""
    global _proc
    if _proc is None:
        return
    if _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _proc.kill()
    _proc = None
