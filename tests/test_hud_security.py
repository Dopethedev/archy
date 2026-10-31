"""The dashboard is a control surface, so it has to be treated as one.

Being on loopback is not protection: any page in the browser can POST to a
local port as a "simple request" that skips the preflight, and the assistant
runs shell commands. These are the guards, plus the check that a legitimate
page still gets through.
"""
import json, re, socket, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from archy import config
from archy.hud import Hud

PORT = 8799
HOST = config.HUD_HOST
hud = Hud(port=PORT)
said: list[str] = []
mic_calls: list[bool] = []
hud.on_say = said.append
hud.on_mic = mic_calls.append
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


def post(body: str, token=None, origin=None, ctype="application/json",
         path="/say") -> str:
    head = (f"POST {path} HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
            f"Content-Type: {ctype}\r\nContent-Length: {len(body)}\r\n")
    if token is not None:
        head += f"X-Archy-Token: {token}\r\n"
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

# ------------------------------------------------------------ the mic toggle is guarded too
# The microphone is the other way in, so it needs the same treatment. A page
# that could switch it on could listen to the room.
before_mic = len(mic_calls)
resp = post(json.dumps({"on": True}), token=None, origin="https://evil.example",
            ctype="text/plain", path="/mic")
time.sleep(0.3)
check("a random website cannot open the microphone",
      len(mic_calls) == before_mic,
      f"server said {resp.splitlines()[0] if resp else 'nothing'}; reached it: {mic_calls[before_mic:]}")

resp = post(json.dumps({"on": False}), token="not-the-token", path="/mic")
time.sleep(0.3)
check("the microphone toggle needs the token",
      len(mic_calls) == before_mic, f"server said {resp.splitlines()[0] if resp else 'nothing'}")

resp = post(json.dumps({"on": False}), token=hud.token,
            origin=f"http://{HOST}:{PORT}", path="/mic")
time.sleep(0.3)
check("the dashboard can switch the microphone",
      mic_calls[before_mic:] == [False],
      f"server said {resp.splitlines()[0] if resp else 'nothing'}; got {mic_calls[before_mic:]}")

resp = post(json.dumps({"on": True}), token=hud.token,
            origin=f"http://{HOST}:{PORT}", path="/nonsense")
check("an unknown control path is not a control path",
      resp.startswith("HTTP/1.1 404"), resp.splitlines()[0] if resp else "no response")

# ------------------------------------------------------------ static files stay inside hud/
sibling = Path(config.HUD_DIR).parent / "hud_evil"
sibling.mkdir(exist_ok=True)
(sibling / "secret.txt").write_text("served from outside the hud directory")
resp = raw(f"GET /../hud_evil/secret.txt HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
           f"Connection: close\r\n\r\n")
check("a file next to hud/ cannot be served",
      "served from outside" not in resp,
      resp.splitlines()[0] if resp else "no response")
resp = raw(f"GET /../src/archy/hud.py HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
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

# ------------------------------------------------------------ a header is bytes, not ascii
# Header values are read as latin-1, so a token carrying anything outside ascii
# used to make the comparison raise TypeError: a traceback and a dropped
# connection where a 403 was meant to be. Sent as bytes, the way it arrives.
def raw_bytes(request: bytes, settle: float = 1.0) -> str:
    out = b""
    with socket.create_connection((HOST, PORT), timeout=5) as s:
        s.sendall(request)
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


resp = raw_bytes((f"GET /events HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
                   f"X-Archy-Token: tok\u00e9\r\nConnection: close\r\n\r\n")
                  .encode("latin-1"))
check("a token with non-ascii in it is refused, not a fault",
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
      f'"{hud.token}"' in page and "__ARCHY_TOKEN__" not in page,
      "placeholder replaced" if hud.token in page else "TOKEN MISSING FROM PAGE")

# and it says your wake phrase, not the one the repository was written with
real_phrase = config.WAKE_PHRASE
try:
    config.WAKE_PHRASE = "hey tesla"
    page = raw(f"GET / HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nConnection: close\r\n\r\n")
    check("the page shows the configured wake phrase",
          "hey tesla" in page and "__ARCHY_WAKE__" not in page
          and page.count("hey tesla") >= 2,
          f"placeholder left in: {'__ARCHY_WAKE__' in page}")
finally:
    config.WAKE_PHRASE = real_phrase

# The page that ships in the repository must not carry a phrase of its own: it
# carries a placeholder, and the fallback for a page served without the swap is
# in the script. A hardcoded phrase here leaks whoever's it was, and it did once.
page_path = Path(__file__).resolve().parents[1] / "hud" / "index.html"
js_path = Path(__file__).resolve().parents[1] / "hud" / "app.js"
hint_page = page_path.read_text()
hint_js = js_path.read_text()
check("the page in the repository carries a placeholder, not a phrase",
      "__ARCHY_WAKE__" in hint_page and "hey archy" not in hint_page,
      "the phrase lives in config.py and the fallback in app.js")

# The rename was a blanket one and it missed this page: the boot title and the
# header brand still said the old name, which is the name every picture of the
# page carried into the README, the release page and the demo film. It was spaced
# out letter by letter, so the whitespace comes out before matching: the literal
# text was "J A R V I S", which a search for the name does not find.
old_name = [path.name for path in sorted(page_path.parent.glob("*"))
            if path.suffix in (".html", ".js", ".css")
            and "jarvis" in re.sub(r"\s+", "", path.read_text()).lower()]
check("nothing in the dashboard answers to the old name", not old_name,
      ", ".join(old_name))
check("a page served without the swap falls back instead of showing it",
      "/^__.*__$/" in hint_js and 'dataset.wake || "hey archy"' not in hint_js,
      "the script refuses a leftover placeholder")

# ------------------------------------------------------------ a bad body is refused quickly
resp = raw(f"POST /say HTTP/1.1\r\nHost: {HOST}:{PORT}\r\n"
           f"X-Archy-Token: {hud.token}\r\nContent-Type: application/json\r\n"
           f"Content-Length: 9999999\r\nConnection: close\r\n\r\n")
check("an oversized body is refused instead of blocking a thread",
      resp.startswith("HTTP/1.1 413"), resp.splitlines()[0] if resp else "no response")

# valid JSON that is not an object used to reach .get() and raise, which the
# client sees as a server fault rather than a refusal
for body in ('["a", "list"]', '"a bare string"', "42"):
    resp = post(body, token=hud.token)
    check(f"a body that is not an object is refused: {body[:14]}",
          resp.startswith("HTTP/1.1 400"), resp.splitlines()[0])

# a control request still works with the token, so the refusals above are not
# just a broken endpoint
resp = post('{"text": "hello"}', token=hud.token)
check("a well formed request is accepted", resp.startswith("HTTP/1.1 200"),
      resp.splitlines()[0] if resp else "no response")

# ------------------------------------------------------------ a port already in use
# The failure a second copy hits, and the advice it prints. The advice has to name
# a command that matches how this program is actually started, which is what the
# old name stopped doing: it told the reader to kill a process that does not exist.
taken = socket.socket()
taken.bind((HOST, 0))
taken.listen(1)
taken_port = taken.getsockname()[1]
try:
    notice = None
    try:
        Hud(port=taken_port).start()
    except RuntimeError as exc:
        notice = str(exc)
    check("a taken port is reported, not swallowed", notice is not None, str(notice))
    check("and it names the program, not its old name",
          bool(notice) and "archy" in notice and "jarvis" not in notice.lower(),
          str(notice))
    suggested = re.search(r"pkill -f '([^']+)'", notice or "")
    check("the pattern it suggests matches the way this runs",
          bool(suggested) and bool(re.search(suggested.group(1), "python -m archy")),
          str(suggested and suggested.group(1)))
finally:
    taken.close()

hud.stop()
(sibling / "secret.txt").unlink(missing_ok=True)
sibling.rmdir()
print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
