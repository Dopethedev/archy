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
import html
import json
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, permissions, sysinfo

MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}

TOKEN_PLACEHOLDER = "__ARCHY_TOKEN__"
# the wake phrase is yours, so the page is told it rather than carrying a copy of
# whatever was on the machine it was written on
PHRASE_PLACEHOLDER = "__ARCHY_WAKE__"
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
        supplied = self.headers.get("X-Archy-Token") or ""
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
            body = body.replace(PHRASE_PLACEHOLDER.encode(),
                                html.escape(config.WAKE_PHRASE).encode())
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
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._send(400, b"bad request", "text/plain")
            return None
        if not isinstance(payload, dict):
            # a bare list or string is valid JSON and would turn into an
            # AttributeError on the next .get(), which reads as a server fault
            self._send(400, b"an object, please", "text/plain")
            return None
        return payload


class Hud:
    def __init__(self, port: int = config.HUD_PORT):
        self.port = port
        self.token = secrets.token_urlsafe(24)     # new every run, never on disk
        self.subscribers: list[queue.Queue] = []
        self.replay: list[dict] = []          # last few events, to prime a new page
        self.last_state: dict | None = None   # kept apart: it must never be evicted
        self.on_say = None                    # set by the assistant: callable(str)
        self.on_mic = None                    # set by the assistant: callable(bool)
        # set by the assistant when the status panel is switched on: callable() -> dict
        # None means the panel is off, which is the dashboard exactly as it was.
        self.status_source = None
        self._ticks = 0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ events
    def publish(self, event: dict):
        kind = event.get("type")
        with self._lock:
            if kind == "state":
                self.last_state = event
            if kind in ("state", "stats", "status"):
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
        """What a page that just connected needs.

        The state, the latest stats, the last heard caption and the theme: the
        things that describe right now. The activity log is deliberately left
        out, so a freshly opened window starts clean instead of replaying the
        whole afternoon."""
        with self._lock:
            keep = {"state", "stats", "heard", "mic", "theme", "turn"}
            head = [self.last_state] if self.last_state else []
            rest = [e for e in self.replay
                    if e is not self.last_state and e.get("type") in keep]
            return head + rest[-12:]

    def subscribe(self, chan: queue.Queue):
        with self._lock:
            self.subscribers.append(chan)

    def unsubscribe(self, chan: queue.Queue):
        with self._lock:
            if chan in self.subscribers:
                self.subscribers.remove(chan)

    # ------------------------------------------------------------------ system
    # Every reading comes from sysinfo, which is what knows this is a mac or a
    # Linux box. These three keep their names because the dials are wired to
    # them, nothing else.
    _cpu_percent = staticmethod(sysinfo.cpu_percent)
    _memory = staticmethod(sysinfo.memory)
    _temp = staticmethod(sysinfo.temperature_c)

    def _stats_loop(self):
        prev = (0, 0)
        self._cpu_percent(prev)
        while not self._stop.wait(1.5):
            self._ticks += 1
            # One reading that raises used to end this thread for good: the dials
            # would freeze at their last numbers and the status panel would stop
            # updating, with nothing on the page saying anything had stopped. A
            # dashboard that lies by standing still is worse than one that skips a
            # tick, so a failure is reported and the loop carries on.
            try:
                cpu, prev = self._cpu_percent(prev)
                mem_pct, mem_gb = self._memory()
                temp = self._temp()
                self.publish({"type": "stats", "cpu": round(cpu, 1),
                              "mem": round(mem_pct, 1), "mem_gb": round(mem_gb, 1),
                              "temp": round(temp, 1) if temp else None})
            except Exception as exc:
                print(f"[hud] could not read the machine: {type(exc).__name__}: {exc}",
                      flush=True)
            try:
                self._publish_status()
            except Exception as exc:
                print(f"[hud] could not publish the status: {exc}", flush=True)

    def _publish_status(self):
        """The assistant's own state, on the same clock as the machine's.

        Not every tick: a waiting list read and a privacy state every second and a
        half is work nobody asked for. Every third tick, and the first one, so a
        page opened just now is not blank for five seconds.

        A source that fails is reported as unavailable rather than dropped, because
        a panel that silently stops updating is worse than one that says it cannot
        read something.
        """
        if self.status_source is None or self._ticks % config.STATUS_EVERY_TICKS != 1:
            return
        try:
            reading = self.status_source()
        except Exception as exc:
            reading = {"unavailable": permissions.redact(f"{type(exc).__name__}: {exc}")}
        self.publish({"type": "status", **reading})

    # -------------------------------------------------------------------- life
    def start(self):
        try:
            self._server = ThreadingHTTPServer((config.HUD_HOST, self.port), _Handler)
        except OSError as exc:
            raise RuntimeError(
                f"dashboard port {self.port} is already in use ({exc}). "
                # both ways this runs: ./run.sh, which is python -m archy, and the
                # console script. The bracket is so the pattern does not match the
                # shell that is running the command, which is how it kills itself.
                f"Another copy of archy is probably running: "
                f"pkill -f '[p]ython -m archy|[b]in/archy'"
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
