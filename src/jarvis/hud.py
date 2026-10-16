"""The dashboard: a local web page fed by server-sent events.

Standard library only. It listens on loopback, serves the static files from
hud/, and pushes state changes, audio levels, the transcript and machine
stats to whatever browser is open.

Being on loopback is not by itself protection, because this is a control
surface for something that runs commands. Any page in the browser can POST to
a local port as a "simple request" without a preflight, so a request is only
accepted when it carries the token that came with the page, arrives as JSON,
and names the host we actually serve. The token is minted per run and never
written to disk.
"""

from __future__ import annotations

import hmac
import json
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config

MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}

TOKEN_PLACEHOLDER = "__JARVIS_TOKEN__"
MAX_BODY = 8192                          # a typed instruction, not a file upload
LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass                                  # keep the console clean

    def _send(self, code: int, body: bytes, ctype: str):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                              # the client left; not worth a traceback

    # ------------------------------------------------------------------ checks
    @property
    def _hud(self):
        return self.server.hud                                    # type: ignore[attr-defined]

    def _host_ok(self) -> bool:
        """Reject a name that is not how we are reached.

        A page can point its own domain at this loopback port (DNS rebinding)
        and then be "same origin" with the server, which would expose the
        transcript and the token. The Host header is what gives it away.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_port
        return host in {f"{name}:{port}" for name in LOOPBACK_NAMES} | set(LOOPBACK_NAMES)

    def _origin_ok(self) -> bool:
        """A cross-site request is not the page we served."""
        origin = (self.headers.get("Origin") or "").strip().lower()
        if not origin:
            return True                       # curl and other local tools send none
        host = (self.headers.get("Host") or "").strip().lower()
        return origin in (f"http://{host}", f"https://{host}")

    def _token_ok(self) -> bool:
        """The token from the served page, in a header or in the query.

        A query parameter exists because EventSource cannot set headers, and
        the event stream carries the transcript.
        """
        supplied = self.headers.get("X-Jarvis-Token") or ""
        if not supplied and "?" in self.path:
            from urllib.parse import parse_qs
            supplied = (parse_qs(self.path.split("?", 1)[1]).get("t") or [""])[0]
        return hmac.compare_digest(supplied, self._hud.token)

    # -------------------------------------------------------------------- get
    def do_GET(self):
        hud = self._hud
        if not self._host_ok():
            self._send(403, b"wrong host", "text/plain")
            return
        path = self.path.split("?")[0]

        if path == "/events":
            if not self._token_ok():
                self._send(403, b"not allowed", "text/plain")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            chan: queue.Queue = queue.Queue(maxsize=200)
            try:
                # subscribe before catching up: an event that lands in between
                # arrives twice at worst, whereas the other order loses it, and
                # a lost state change leaves the page lying about what is
                # happening
                hud.subscribe(chan)
                # the current state goes first and is never evicted, so a tab
                # opened later is correct immediately rather than at the next
                # change
                for ev in hud.catch_up():
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        ev = chan.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hud.unsubscribe(chan)
            return

        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (config.HUD_DIR / name).resolve()
        # is_relative_to, not a string prefix: "hud_old" also starts with "hud"
        if not target.is_relative_to(config.HUD_DIR.resolve()) or not target.is_file():
            self._send(404, b"not found", "text/plain")
            return
        body = target.read_bytes()
        if target.name == "index.html":
            body = body.replace(TOKEN_PLACEHOLDER.encode(), hud.token.encode())
        self._send(200, body, MIME.get(target.suffix, "application/octet-stream"))

    # ------------------------------------------------------------------- post
    def do_POST(self):
        """Typed input and microphone control. Only the served page gets in."""
        path = self.path.split("?")[0]
        if path not in ("/say", "/mic"):
            self._send(404, b"not found", "text/plain")
            return
        payload = self._json_body()
        if payload is None:
            return                            # already answered with the reason
        hud = self._hud
        if path == "/say":
            text = str(payload.get("text", "")).strip()
            if text and hud.on_say:
                hud.on_say(text)
        else:
            on = bool(payload.get("on", True))
            if hud.on_mic:
                hud.on_mic(on)
        self._send(200, b'{"ok":true}', "application/json")

    def _json_body(self) -> dict | None:
        """The checks every control request passes, or None once it is refused.

        A request without the served token, or from another page, or not
        speaking JSON, is turned away before it can reach the assistant. Any
        page in the browser can POST to a local port, so the token is the only
        thing standing between a web page and a command surface.
        """
        if not (self._host_ok() and self._origin_ok() and self._token_ok()):
            self._send(403, b"not allowed", "text/plain")
            return None
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send(415, b"application/json only", "text/plain")
            return None
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY:
            self._send(413, b"bad length", "text/plain")
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, b"bad request", "text/plain")
            return None


class Hud:
    def __init__(self, port: int = config.HUD_PORT):
        self.port = port
        self.token = secrets.token_urlsafe(24)     # new every run, never on disk
        self.subscribers: list[queue.Queue] = []
        self.replay: list[dict] = []          # last few events, to prime a new page
        self.last_state: dict | None = None   # kept apart: it must never be evicted
        self.on_say = None                    # set by the assistant: callable(str)
        self.on_mic = None                    # set by the assistant: callable(bool)
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ events
    def publish(self, event: dict):
        kind = event.get("type")
        with self._lock:
            if kind == "state":
                self.last_state = event
            if kind in ("state", "stats"):
                # the latest of these is the only one worth replaying
                self.replay = [e for e in self.replay if e.get("type") != kind]
            # A level is a moment, not history. Keeping them would fill the whole
            # replay buffer with the last half second of microphone level and
            # evict the transcript, which is the part a fresh tab needs.
            if kind != "level":
                self.replay.append(event)
                del self.replay[:-40]
            subscribers = list(self.subscribers)
        for chan in subscribers:
            try:
                chan.put_nowait(event)
            except queue.Full:
                pass                          # a slow tab misses a frame, nothing more

    def catch_up(self) -> list[dict]:
        """What a page that just connected needs: the truth, then recent history."""
        with self._lock:
            head = [self.last_state] if self.last_state else []
            return head + [e for e in self.replay if e is not self.last_state]

    def subscribe(self, chan: queue.Queue):
        with self._lock:
            self.subscribers.append(chan)

    def unsubscribe(self, chan: queue.Queue):
        with self._lock:
            if chan in self.subscribers:
                self.subscribers.remove(chan)

    # ------------------------------------------------------------------ system
    @staticmethod
    def _cpu_percent(prev: tuple[int, int]) -> tuple[float, tuple[int, int]]:
        try:
            with open("/proc/stat") as fh:
                parts = [int(x) for x in fh.readline().split()[1:]]
            idle, total = parts[3] + parts[4], sum(parts)
            d_idle, d_total = idle - prev[0], total - prev[1]
            pct = 0.0 if d_total <= 0 else 100.0 * (1 - d_idle / d_total)
            return pct, (idle, total)
        except Exception:
            return 0.0, prev

    @staticmethod
    def _memory() -> tuple[float, float]:
        try:
            info = {}
            with open("/proc/meminfo") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    info[key] = float(rest.strip().split()[0])
            total, avail = info.get("MemTotal", 1), info.get("MemAvailable", 0)
            return 100.0 * (1 - avail / total), (total - avail) / 1024 / 1024
        except Exception:
            return 0.0, 0.0

    @staticmethod
    def _temp() -> float | None:
        for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
            try:
                if (hwmon / "name").read_text().strip() not in ("coretemp", "k10temp", "zenpower"):
                    continue
                for label in sorted(hwmon.glob("temp*_label")):
                    if "package" in label.read_text().lower():
                        return int((hwmon / label.name.replace("_label", "_input")).read_text()) / 1000
            except Exception:
                continue
        return None

    def _stats_loop(self):
        prev = (0, 0)
        self._cpu_percent(prev)
        while not self._stop.wait(1.5):
            cpu, prev = self._cpu_percent(prev)
            mem_pct, mem_gb = self._memory()
            temp = self._temp()
            self.publish({"type": "stats", "cpu": round(cpu, 1), "mem": round(mem_pct, 1),
                          "mem_gb": round(mem_gb, 1), "temp": round(temp, 1) if temp else None})

    # -------------------------------------------------------------------- life
    def start(self):
        try:
            self._server = ThreadingHTTPServer((config.HUD_HOST, self.port), _Handler)
        except OSError as exc:
            raise RuntimeError(
                f"dashboard port {self.port} is already in use ({exc}). "
                f"Another copy of jarvis is probably running: pkill -f '[j]arvis'"
            ) from exc
        self._server.hud = self                                # type: ignore[attr-defined]
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        threading.Thread(target=self._stats_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    @property
    def url(self) -> str:
        return f"http://{config.HUD_HOST}:{self.port}/"
