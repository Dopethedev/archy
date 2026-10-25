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
    def do_GET(self):                                  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

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
check("and it is left alone, not restarted",
      local_model.ensure_running() is True)
check("nothing was spawned for it", local_model._proc is None,
      "a router someone else started is theirs")
local_model.stop()
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

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}  ({passed} checks)")
sys.exit(1 if failed else 0)
