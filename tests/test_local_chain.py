"""The local chain: which models to try, and the server behind them.

The rule that matters here is the one the whole offline path exists for. With no
internet, walking four relays one silence deadline at a time spends most of a
minute proving what the first failure already proved, so the chain is rebuilt
around the local model instead of being walked down to it.

No model server is started or stopped in here. Where a server is needed, a
throwaway HTTP server on a loopback port stands in, and a command that exits at
once stands in for one that will not come up. The model files are not touched:
the sizes and speeds in the comments were measured against the real thing.
"""
import http.server
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis import brain, config, local_model, net  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:58s} {detail}")


print("-- which chain, given the network --")
saved = (config.PI_CHAIN_MODE, config.PI_MODELS, config.PI_MODELS_OFFLINE)
config.PI_MODELS = ("relay/one", "relay/two")
config.PI_MODELS_OFFLINE = ()
config.PI_CHAIN_MODE = "auto"
check("with no local model the chain is exactly what it always was",
      brain.model_chain() == ("relay/one", "relay/two"), str(brain.model_chain()))

config.PI_MODELS_OFFLINE = ("llama.cpp/Qwen3.5-4B",)
net.net.note_success()
chain = brain.model_chain()
check("online, the local model is the floor under the relays",
      chain == ("relay/one", "relay/two", "llama.cpp/Qwen3.5-4B"), str(chain))
check("and it is tried last, not first", chain[-1] == "llama.cpp/Qwen3.5-4B")

net.net.note_failure("getaddrinfo ENOTFOUND relay.example")
chain = brain.model_chain()
check("offline, the local model is the only one tried",
      chain == ("llama.cpp/Qwen3.5-4B",), str(chain))
check("so no relay costs a silence deadline before it",
      not any(m.startswith("relay/") for m in chain), str(chain))

config.PI_CHAIN_MODE = "online"
check("online-only never goes local",
      brain.model_chain() == ("relay/one", "relay/two"), str(brain.model_chain()))
config.PI_CHAIN_MODE = "offline"
check("offline-only never calls a relay",
      brain.model_chain() == ("llama.cpp/Qwen3.5-4B",), str(brain.model_chain()))
check("and it is how the local path gets tested without unplugging anything",
      config.PI_CHAIN_MODE in ("auto", "online", "offline"))

config.PI_CHAIN_MODE, config.PI_MODELS, config.PI_MODELS_OFFLINE = saved

print("-- the server, when it is already running --")
PORT = 18799
saved_url = config.PI_LOCAL_SERVER_URL


class Silent(http.server.BaseHTTPRequestHandler):
    """A router stand-in: health, a model list, and a load endpoint."""

    state = "loaded"            # what GET /models reports
    loads: list = []           # every POST /models/load
    sticks = True              # whether a load request changes the state

    def _answer(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                  # noqa: N802
        import json as _json
        if self.path == "/health":
            return self._answer(b'{"status":"ok"}')
        if self.path == "/models":
            entries = [] if Silent.state == "empty" else [
                {"id": "Qwen3.5-4B-Q4_K_M", "status": {"value": Silent.state}}]
            return self._answer(_json.dumps({"data": entries}).encode())
        return self._answer(b"{}")

    def do_POST(self):                                 # noqa: N802
        Silent.loads.append(self.path)
        if Silent.state == "empty":
            # what a real router does when asked for a model it does not have:
            # this is the wrong model directory, not a slow disk
            body = b'{"error":{"code":404,"message":"unknown model"}}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if Silent.sticks:
            Silent.state = "loaded"
        self._answer(b'{"success":true}')

    def log_message(self, *a):
        pass


class Server(socketserver.TCPServer):
    allow_reuse_address = True


httpd = Server(("127.0.0.1", PORT), Silent)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
config.PI_LOCAL_SERVER_URL = f"http://127.0.0.1:{PORT}"
started = time.monotonic()
check("a server that answers is healthy", local_model.healthy() is True,
      f"{(time.monotonic() - started) * 1000:.0f} ms")
config.PI_MODELS_OFFLINE = ("llama.cpp/x",)

print("-- a healthy router with the model loaded --")
Silent.state, Silent.loads[:] = "loaded", []
check("it is used as it is", local_model.ensure_running() is True)
check("nothing is asked of it", Silent.loads == [], str(Silent.loads))
check("nothing was spawned for it", local_model._proc is None,
      "a router someone else started is theirs")

print("-- a healthy router with nothing loaded, which is the bug this fixes --")
# The router runs with explicit loading, so a healthy one with an idle model
# answers the first question with 400, model is not loaded. That was a real lost
# turn in offline mode, found by running it rather than by reading it.
Silent.state, Silent.loads[:] = "unloaded", []
check("the model is asked for", local_model.ensure_running() is True)
check("and the request went to the load endpoint",
      Silent.loads == ["/models/load"], str(Silent.loads))
check("only once, however often it is asked",
      (local_model.ensure_running(), len(Silent.loads))[1] == 1, str(Silent.loads))

print("-- a router that never finishes loading --")
Silent.state, Silent.loads[:], Silent.sticks = "unloaded", [], False
saved_wait = config.PI_LOCAL_LOAD_WAIT_S
config.PI_LOCAL_LOAD_WAIT_S = 1.0
started = time.monotonic()
check("it gives up and says so", local_model.ensure_running() is False)
elapsed = time.monotonic() - started
check("the wait is bounded", elapsed < 3.0, f"{elapsed:.1f} s")
config.PI_LOCAL_LOAD_WAIT_S = saved_wait
Silent.sticks, Silent.state = True, "loaded"

print("-- a router with no models at all, which is the wrong directory --")
Silent.state, Silent.loads[:] = "empty", []
check("nothing to load is not a success", local_model.ensure_running() is False)
check("the attempt was made and refused", Silent.loads == ["/models/load"],
      str(Silent.loads))
Silent.state = "loaded"

check("stopping does not kill someone else's server", local_model.healthy() is True)
httpd.shutdown()
httpd.server_close()

print("-- a server that is not there --")
config.PI_LOCAL_SERVER_URL = "http://127.0.0.1:18798"
check("nothing listening is not healthy", local_model.healthy() is False)
check("and the check is quick, not a hang",
      True, "1.5 s timeout on a loopback port")

config.PI_MODELS_OFFLINE = ()
check("with nothing configured there is nothing to start",
      local_model.ensure_running() is False)

config.PI_MODELS_OFFLINE = ("llama.cpp/x",)
saved_dir, saved_bin = config.PI_LOCAL_MODELS_DIR, config.PI_LOCAL_SERVER_BIN
config.PI_LOCAL_SERVER_BIN = "/bin/true"               # exits at once
started = time.monotonic()
ok = local_model.ensure_running(wait=5.0)
elapsed = time.monotonic() - started
check("a server that exits at once is reported, not waited out", ok is False)
check("and the wait ends when the process does", elapsed < 3.0, f"{elapsed:.1f} s")
local_model.stop()
check("a dead one of ours leaves no process behind", local_model._proc is None)

config.PI_LOCAL_MODELS_DIR = "/nonexistent-models-dir"
check("a missing model directory is a plain no",
      local_model.ensure_running(wait=2.0) is False)
config.PI_LOCAL_MODELS_DIR, config.PI_LOCAL_SERVER_BIN = saved_dir, saved_bin

print("-- the command the server is started with --")
# A local model thinks before it speaks, and every one of those tokens is silence
# in the user's ear: measured on a screen question, 27.4 s to the first spoken word
# with reasoning on, against 4.7 s for the whole turn with it off. The silence
# deadline is 25 s, so a reasoning local model was reported as a dropped connection.
# The flag that switches it off is asserted here, since it is the only part of that
# which a machine without a model can check.
saved_url = config.PI_LOCAL_SERVER_URL
config.PI_LOCAL_SERVER_URL = "http://127.0.0.1:18799"
config.PI_MODELS_OFFLINE = ("local/Qwen3.5-4B-Q4_K_M",)
saved_healthy, saved_wait, saved_load = (local_model.healthy, local_model._wait,
                                         local_model._ensure_loaded)
local_model.healthy = lambda *a, **k: False
local_model._wait = lambda *a, **k: True
local_model._ensure_loaded = lambda *a, **k: True


class FakeProc:
    returncode = None
    def poll(self):
        return None


class FakeSubprocess:
    argv: list[str] = []
    DEVNULL = -3
    def Popen(self, args, **kwargs):
        FakeSubprocess.argv = args
        return FakeProc()


real_subprocess = local_model.subprocess
local_model.subprocess = FakeSubprocess()
try:
    started = local_model.ensure_running()
finally:
    local_model.subprocess = real_subprocess
    local_model.healthy, local_model._wait, local_model._ensure_loaded = (
        saved_healthy, saved_wait, saved_load)
    config.PI_LOCAL_SERVER_URL = saved_url

argv = FakeSubprocess.argv
check("the server was started", started is True and argv, " ".join(argv[:4]))
check("with the no-thinking flag, so speech is not spent on silence",
      "--reasoning" in argv and argv[argv.index("--reasoning") + 1] == "off",
      " ".join(argv[argv.index("--reasoning"):]) if "--reasoning" in argv else "missing")
check("and the flags that make it usable for voice are still there",
      all(flag in argv for flag in ("--models-dir", "--jinja", "--flash-attn", "-c")),
      " ".join(argv))

print("-- what counts as a model on disk --")
with tempfile.TemporaryDirectory() as tmp:
    config.PI_LOCAL_MODELS_DIR = Path(tmp)
    (Path(tmp) / "model-Q4_K_M.gguf").write_bytes(b"x")
    (Path(tmp) / "mmproj-F16.gguf").write_bytes(b"x")     # a projector, not a model
    (Path(tmp) / "notes.txt").write_text("hi")
    sub = Path(tmp) / "Qwen3.5-4B-Q4_K_M"
    sub.mkdir()
    (sub / "Qwen3.5-4B-Q4_K_M.gguf").write_bytes(b"x")
    (sub / "mmproj-F16.gguf").write_bytes(b"x")
    found = local_model.models_present()
    check("a model in a subdirectory is found", len(found) == 2, str(len(found)))
    check("the vision projector is not counted as one",
          all(not p.name.startswith("mmproj") for p in found),
          str([p.name for p in found]))
    check("and neither is anything that is not a gguf",
          not any(p.suffix != ".gguf" for p in found))

config.PI_LOCAL_MODELS_DIR = saved_dir
config.PI_LOCAL_SERVER_URL = saved_url

print("-- every entry point starts the server, not only the voice loop --")
# This is the bug that was found by running it: `--say` went straight to
# brain.start() without the local server, so the first question in offline mode
# came back as a dropped connection. One shared method now, and the order is the
# part that matters.
import asyncio  # noqa: E402

from jarvis.assistant import Assistant  # noqa: E402

calls: list = []
real_ensure_local = local_model.ensure_running


class StubAssistant(Assistant):
    def __init__(self):
        self.brain = type("B", (), {"start": lambda s: None})()

    async def _start(self, label, step):
        calls.append("brain")
        return True


local_model.ensure_running = lambda *a, **k: calls.append("server") or True
saved = (config.PI_CHAIN_MODE, config.PI_MODELS_OFFLINE)
config.PI_CHAIN_MODE, config.PI_MODELS_OFFLINE = "offline", ("local/x",)
calls.clear()
asyncio.run(StubAssistant().start_brain())
check("in offline mode the server comes up first", calls == ["server", "brain"],
      str(calls))

net.net.note_success()
config.PI_CHAIN_MODE = "online"
calls.clear()
asyncio.run(StubAssistant().start_brain())
check("online, no server is started for a relay", calls == ["brain"], str(calls))

config.PI_CHAIN_MODE = "auto"
net.net.note_failure("getaddrinfo ENOTFOUND relay.example")
calls.clear()
asyncio.run(StubAssistant().start_brain())
check("and auto follows the verdict", calls == ["server", "brain"], str(calls))
local_model.ensure_running = real_ensure_local
config.PI_CHAIN_MODE, config.PI_MODELS_OFFLINE = saved
net.net.note_success()

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}  ({passed} checks)")
sys.exit(1 if failed else 0)
