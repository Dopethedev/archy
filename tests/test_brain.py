"""Brain test: drive `pi --mode rpc` and measure voice-relevant latency."""
import json, subprocess, sys, threading, time, queue

VOICE_RULES = (
    "Your replies are spoken aloud through a speaker, so answer in one or two "
    "short sentences of plain text. Never use markdown, code fences, bullet "
    "lists, headings, emoji, or URLs. If the user asks you to do something on "
    "the computer, do it with your tools, then say briefly what you did."
)

args = ["pi", "--mode", "rpc", "--no-session",
        "--append-system-prompt", VOICE_RULES,
        "--thinking", "off"]

t0 = time.monotonic()
proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True, bufsize=1)

lines = queue.Queue()
def reader():
    for line in proc.stdout:
        lines.put(line)
threading.Thread(target=reader, daemon=True).start()

def send(obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()

def read_until(kind, timeout):
    first_delta = None
    tools = []
    text = ""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=max(0.05, deadline - time.monotonic()))
        except queue.Empty:
            break
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "tool_execution_start":
            tools.append(ev.get("toolName"))
        if ev.get("type") == "message_update":
            ame = ev.get("assistantMessageEvent", {})
            if ame.get("type") == "text_delta":
                if first_delta is None:
                    first_delta = time.monotonic() - t0
                text += ame.get("delta", "")
        if ev.get("type") == kind:
            return first_delta, text, tools, True
    return first_delta, text, tools, False

send({"type": "prompt", "message": "what is my cpu usage right now"})
fd, text, tools, done = read_until("agent_end", 90)
print(f"startup+first token : {fd:.2f}s" if fd else "no text received")
print(f"total to agent_end  : {time.monotonic()-t0:.2f}s  (done={done})")
print(f"tools used          : {tools or 'none'}")
print(f"reply               : {text.strip()[:300]!r}")

if not text.strip():
    print("FAIL: no reply")
    proc.kill(); sys.exit(1)

# does memory persist across turns in one process?
t = time.monotonic()
send({"type": "prompt", "message": "what number did I just mention? no, ignore that. just say ok"})
fd2, text2, _, done2 = read_until("agent_end", 90)
print(f"\nsecond turn         : {time.monotonic()-t:.2f}s  reply={text2.strip()[:120]!r}")
print("\nPASS: pi answers over rpc")
proc.kill()
