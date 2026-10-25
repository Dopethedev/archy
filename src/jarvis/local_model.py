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

import json
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


def _router_model_id() -> str:
    """The router's name for the configured model.

    The chain holds ids the way pi wants them (`local/Qwen3.5-4B-Q4_K_M`), and the
    router knows the model as the part after the provider prefix, which is also
    what it reported in `/models` before any of this existed.
    """
    first = str(config.PI_MODELS_OFFLINE[0]) if config.PI_MODELS_OFFLINE else ""
    return first.split("/", 1)[1] if "/" in first else first


def _post(path: str, payload: dict, timeout: float = 5.0) -> bool:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(f"{config.PI_LOCAL_SERVER_URL}{path}", data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def loaded(timeout: float = 1.5) -> bool | None:
    """Is a model loaded? None when the router cannot be asked."""
    try:
        with urllib.request.urlopen(f"{config.PI_LOCAL_SERVER_URL}/models",
                                    timeout=timeout) as resp:
            data = json.loads(resp.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    entries = data.get("data") or []
    if not entries:
        return False
    state = (entries[0].get("status") or {}).get("value")
    return state in ("loaded", "sleeping")


def ensure_running(wait: float = 40.0) -> bool:
    """Start the router if it is not up, load the model, and say whether it is usable.

    A router someone else started is left alone, apart from the model being loaded
    if it is idle: the router runs with explicit loading, so a healthy router with
    nothing loaded answers the first question with `400, model is not loaded`, and
    that is a turn lost for no reason. Loading here also means the wait happens
    once, at start-up, instead of inside somebody's question.

    Both waits are bounded, so a router that never comes up does not hold a turn
    open forever.
    """
    global _proc
    if not config.PI_MODELS_OFFLINE:
        return False                        # nothing configured to run
    model = _router_model_id()
    if healthy():
        return _ensure_loaded(model)
    if _proc is not None and _proc.poll() is None:
        return _wait(wait) and _ensure_loaded(model)
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
    return _wait(wait) and _ensure_loaded(model)


def _ensure_loaded(model: str, wait: float | None = None) -> bool:
    """Ask the router to load the model if it has not, and wait for it."""
    wait = config.PI_LOCAL_LOAD_WAIT_S if wait is None else wait
    state = loaded()
    if state is True:
        return True
    if state is None:
        return False                        # cannot ask, so cannot claim
    print(f"[local] loading {model}", flush=True)
    if not _post("/models/load", {"model": model}):
        print("[local] the load request was refused", flush=True)
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if loaded() is True:
            print(f"[local] {model} is loaded", flush=True)
            return True
        if _proc is not None and _proc.poll() is not None:
            return False                    # the router died while loading
        time.sleep(0.5)
    print(f"[local] {model} did not finish loading within {wait:.0f}s", flush=True)
    return False


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
