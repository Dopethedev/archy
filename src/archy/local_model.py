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


def device_args(listed: str, configured: str) -> list[str]:
    """The device flag, and only when the binary knows that device.

    PI_LOCAL_DEVICE carries the device name on the machine this was written on
    (Vulkan0), and it was passed to every install. On a box with a different
    device name, or none, llama-server refuses to start and the only thing anyone
    ever saw was "the local model did not start", because its output went to
    /dev/null. Offline mode was simply dead there, with no reason next to it.

    The listing is what `llama-server --list-devices` prints:

        Available devices:
          Vulkan0: NVIDIA GeForce RTX 3050 (8192 MiB, 7129 MiB free)

    An empty listing means the binary was not asked, or would not say. The flag is
    passed on then, which is the behaviour from before: a question that cannot be
    answered is not a reason to change what works.
    """
    configured = (configured or "").strip()
    if not configured:
        return []
    names = []
    for line in listed.splitlines():
        name, colon, _ = line.partition(":")
        if colon and " " not in name.strip():
            names.append(name.strip())
    if not names:
        return ["--device", configured]
    if configured in names:
        return ["--device", configured]
    print(f"[local] this machine has no device called {configured!r} "
          f"(it has {', '.join(names)}), so it is not being asked for", flush=True)
    return []


# captured at import, so a stand-in for the subprocess module in a test does not
# have to reproduce the exception hierarchy
_SUBPROCESS_ERRORS = (OSError, subprocess.SubprocessError)


def _devices() -> str:
    """What the binary says it can use, or nothing when it will not say."""
    try:
        out = subprocess.run([config.PI_LOCAL_SERVER_BIN, "--list-devices"],
                             capture_output=True, text=True, timeout=10)
    except _SUBPROCESS_ERRORS as exc:
        print(f"[local] could not ask for the device list: {exc}", flush=True)
        return ""
    return getattr(out, "stdout", "") or ""


def _router_log_path():
    """Where the router's own output goes, so a refusal has a reason."""
    return config.STATE_DIR / "local-server.log"


def _router_log():
    """An appendable log for the child, or /dev/null when it cannot be opened.

    The router's output went to DEVNULL, so every failure it had was invisible:
    "did not come up within 40s" was the whole story, with the actual reason (a
    device that does not exist, a model file it cannot read, a port already
    taken) written nowhere. Kept small, because this is a diagnostic, not a log
    to keep.
    """
    try:
        path = _router_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:
            path.unlink()
        return open(path, "a", encoding="utf-8")
    except OSError as exc:
        print(f"[local] no log for the server: {exc}", flush=True)
        return subprocess.DEVNULL


def _last_words(count: int = 3) -> str:
    """The tail of what the router said, for whoever has to fix it."""
    try:
        lines = _router_log_path().read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    said = [line.strip() for line in lines if line.strip()][-count:]
    return " / ".join(said)[:300]


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
        args += device_args(_devices(), config.PI_LOCAL_DEVICE)
    print(f"[local] starting {' '.join(args)}", flush=True)
    log = _router_log()
    try:
        _proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, text=True)
    except (OSError, ValueError) as exc:
        print(f"[local] could not start {config.PI_LOCAL_SERVER_BIN}: {exc}", flush=True)
        return False
    finally:
        # the child holds its own copy of the descriptor, so this one is only ours
        # to hand over and close; a start is rare but a leaked handle is a leak
        if hasattr(log, "close"):
            log.close()
    if not _wait(wait):
        # the reason is in the log, and it is worth printing: the alternative is
        # "it did not start" with nothing next to it
        said = _last_words()
        print(f"[local] the server said: {said}" if said else
              "[local] the server said nothing", flush=True)
        return False
    return _ensure_loaded(model)


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
            try:
                _proc.wait(timeout=5)      # reaped, not left as a zombie
            except subprocess.TimeoutExpired:
                print("[local] the server would not stop", flush=True)
    _proc = None
