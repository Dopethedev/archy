"""The dashboard is a control surface, so it has to be treated as one.

Being on loopback is not protection: any page in the browser can POST to a
local port as a "simple request" that skips the preflight, and the assistant
runs shell commands. These are the guards, plus the check that a legitimate
page still gets through.
"""
import json, socket, sys, time
from pathlib import Path

sys.path.insert(0, "/home/dope/jarvis/src")
from jarvis import config
from jarvis.hud import Hud

PORT = 8799
HOST = config.HUD_HOST
hud = Hud(port=PORT)
said: list[str] = []
hud.on_say = said.append
hud.start()
time.sleep(0.3)

failures = 0
def check(label, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:52s} {detail}")


def raw(request: str, settle: float = 1.0) -> str:
    """Hand-written request, so nothing is normalised or preflighted for us."""
    out = b""
    with socket.create_connection((HOST, PORT), timeout=5) as s:
        s.sendall(request.encode())
        s.settimeout(settle)
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
        except socket.timeout:
            pass
    return out.decode(errors="replace")


def post(body: str, token=None, origin=None, ctype="application/json") -> str:
    head = (f"POST /say HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
            f"Content-Type: {ctype}\r\nContent-Length: {len(body)}\r\n")
    if token is not None:
        head += f"X-Jarvis-Token: {token}\r\n"
    if origin is not None:
        head += f"Origin: {origin}\r\n"
    return raw(head + "Connection: close\r\n\r\n" + body, settle=0.5)


# ------------------------------------------------------------ a website driving it
# text/plain is a CORS-safelisted type, so the browser sends this with no
# preflight at all. This is the shape a malicious page would use.
before = len(said)
resp = post(json.dumps({"text": "open a terminal"}), token=None,
            origin="https://evil.example", ctype="text/plain")
time.sleep(0.3)
check("a random website cannot drive the assistant",
      len(said) == before, f"server said {resp.splitlines()[0] if resp else 'nothing'}; "
                           f"reached the assistant: {said[before:]}")

# ...and not even with a JSON content type, which would preflight and fail anyway
resp = post(json.dumps({"text": "open a terminal"}), token=None)
time.sleep(0.3)
check("a request without the token is refused",
      len(said) == before, f"server said {resp.splitlines()[0] if resp else 'nothing'}")

# an invented token must not be good enough
resp = post(json.dumps({"text": "open a terminal"}), token="not-the-token",
            origin=f"http://{HOST}:{PORT}")
time.sleep(0.3)
check("a guessed token is refused",
      len(said) == before, f"server said {resp.splitlines()[0] if resp else 'nothing'}")

# ------------------------------------------------------------ the dashboard still works
resp = post(json.dumps({"text": "what is the time"}), token=hud.token,
            origin=f"http://{HOST}:{PORT}")
time.sleep(0.3)
check("the real dashboard still gets through",
      len(said) == before + 1 and said[-1] == "what is the time",
      f"server said {resp.splitlines()[0] if resp else 'nothing'}; got {said[before:]}")

# ------------------------------------------------------------ static files stay inside hud/
sibling = Path(config.HUD_DIR).parent / "hud_evil"
sibling.mkdir(exist_ok=True)
(sibling / "secret.txt").write_text("served from outside the hud directory")
resp = raw(f"GET /../hud_evil/secret.txt HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
           f"Connection: close\r\n\r\n")
check("a file next to hud/ cannot be served",
      "served from outside" not in resp,
      resp.splitlines()[0] if resp else "no response")
resp = raw(f"GET /../src/jarvis/hud.py HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
           f"Connection: close\r\n\r\n")
check("the source cannot be served either",
      "import hmac" not in resp, resp.splitlines()[0] if resp else "no response")

# ------------------------------------------------------------ the transcript is not readable
resp = raw(f"GET /events HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
           f"Origin: https://evil.example\r\nConnection: close\r\n\r\n", settle=1.0)
check("the event stream needs the token",
      resp.startswith("HTTP/1.1 403"), resp.splitlines()[0] if resp else "no response")

# a name that is not ours must be refused, so a rebound domain cannot read it
resp = raw(f"GET /events?t={hud.token} HTTP/1.1\r\nHost: evil.example:{PORT}\r\n"
           f"Connection: close\r\n\r\n", settle=1.0)
check("a rebound hostname is refused",
      resp.startswith("HTTP/1.1 403"), resp.splitlines()[0] if resp else "no response")

# ------------------------------------------------------------ a fresh tab tells the truth
hud.publish({"type": "state", "value": "listening"})
for _ in range(60):                                  # a second of real level traffic
    hud.publish({"type": "level", "mic": 0.2, "out": 0.0})
hud.publish({"type": "state", "value": "idle"})
for _ in range(60):
    hud.publish({"type": "level", "mic": 0.1, "out": 0.0})

resp = raw(f"GET /events?t={hud.token} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
           f"Connection: close\r\n\r\n", settle=1.0)
states = [json.loads(l[6:])["value"] for l in resp.splitlines()
          if l.startswith("data: ") and '"state"' in l]
check("a tab opened now is told the state is idle",
      states[:1] == ["idle"] and states[-1] == "idle",
      f"states it received: {states}")

# the served page carries the token, and it is the real one
page = raw(f"GET / HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nConnection: close\r\n\r\n")
check("the served page carries the live token",
      f'"{hud.token}"' in page and "__JARVIS_TOKEN__" not in page,
      "placeholder replaced" if hud.token in page else "TOKEN MISSING FROM PAGE")

# ------------------------------------------------------------ a bad body is refused quickly
resp = raw(f"POST /say HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
           f"X-Jarvis-Token: {hud.token}\r\nContent-Type: application/json\r\n"
           f"Content-Length: 9999999\r\nConnection: close\r\n\r\n")
check("an oversized body is refused instead of blocking a thread",
      resp.startswith("HTTP/1.1 413"), resp.splitlines()[0] if resp else "no response")

hud.stop()
(sibling / "secret.txt").unlink(missing_ok=True)
sibling.rmdir()
print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
