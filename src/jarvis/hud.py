"""The dashboard: a local web page fed by server-sent events.

Standard library only. It listens on loopback, serves the static files from
hud/, and pushes state changes, audio levels, the transcript and machine
stats to whatever browser is open.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config

MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass                                  # keep the console clean

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        hud = self.server.hud                                   # type: ignore[attr-defined]
        path = self.path.split("?")[0]
        if path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            chan: queue.Queue = queue.Queue(maxsize=200)
            hud.subscribers.append(chan)
            try:
                for ev in hud.replay:                           # catch a fresh tab up
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
                if chan in hud.subscribers:
                    hud.subscribers.remove(chan)
            return

        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (config.HUD_DIR / name).resolve()
        if not str(target).startswith(str(config.HUD_DIR.resolve())) or not target.is_file():
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, target.read_bytes(), MIME.get(target.suffix, "application/octet-stream"))

    def do_POST(self):
        """Typed input, treated exactly like something spoken."""
        hud = self.server.hud                                   # type: ignore[attr-defined]
        if self.path.split("?")[0] != "/say":
            self._send(404, b"not found", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            text = str(payload.get("text", "")).strip()
        except Exception:
            self._send(400, b"bad request", "text/plain")
            return
        if text and hud.on_say:
            hud.on_say(text)
        self._send(200, b'{"ok":true}', "application/json")


class Hud:
    def __init__(self, port: int = config.HUD_PORT):
        self.port = port
        self.subscribers: list[queue.Queue] = []
        self.replay: list[dict] = []          # last few events, to prime a new page
        self.on_say = None                    # set by the assistant: callable(str)
        self._server: ThreadingHTTPServer | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ events
    def publish(self, event: dict):
        if event.get("type") in ("state", "stats"):
            self.replay = [e for e in self.replay if e.get("type") != event["type"]]
        self.replay.append(event)
        del self.replay[:-40]
        for chan in list(self.subscribers):
            try:
                chan.put_nowait(event)
            except queue.Full:
                pass

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
        self._server = ThreadingHTTPServer((config.HUD_HOST, self.port), _Handler)
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
