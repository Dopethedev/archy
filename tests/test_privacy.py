"""What leaves the machine when somebody else's computer is answering.

"It is stored locally" and "it stays local" are different claims, and until this
existed only the first was true. The facts kept about the owner went into every
prompt, files were readable by any tool call, screenshots were attached to whatever
question looked like it wanted one, and yesterday's conversation was resumed by
default. Each of those is now a switch, each switch is checked where the request is
built rather than asked for in the prompt, and the defaults are what the assistant
already did, because a privacy setting that changes behaviour on upgrade is not a
setting.

The checks here read the outbound thing itself: the prompt string that would be
sent, the gate's decision on a real file tool, whether a picture is in the request,
whether `--continue` is on the command line, and which directory holds the
transcripts. Not the settings page.
"""
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import archy.assistant as assistant_mod  # noqa: E402
import archy.brain as brain_mod  # noqa: E402
from archy import config, permissions, privacy  # noqa: E402
from archy.assistant import Assistant  # noqa: E402
from archy.brain import PiBrain  # noqa: E402
from archy.memory_store import Memory  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).resolve().parent / "gate_decide.mjs"

passed = failed = 0


def _raises_keyerror(fn, *args):
    try:
        fn(*args)
        return False
    except KeyError:
        return True


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:58s} {detail}")


work = Path(tempfile.mkdtemp(prefix="archy-privacy-"))
work_root = work / "notes"
work_root.mkdir()
inside = work_root / "diary.txt"
inside.write_text("private things\n")

saved = {name: getattr(config, name) for name in (
    "MEMORY_FILE", "SESSION_DIR", "PI_MODELS_OFFLINE", "PI_CHAIN_MODE", "PI_CONTINUE",
    "PERMISSION_POLICY", "PERMISSION_PENDING", "PERMISSION_APPROVALS", "PERMISSION_LOG",
    "PRIVACY_ONLINE_AI", "PRIVACY_SHARE_MEMORY", "PRIVACY_SHARE_FILES",
    "PRIVACY_SHARE_SCREENS", "PRIVACY_SHARE_HISTORY", "PRIVACY_KEEP_TRANSCRIPTS")}

config.MEMORY_FILE = work / "memory.json"
config.SESSION_DIR = work / "sessions"
config.SESSION_DIR.mkdir()
(config.SESSION_DIR / "2026-01-01.jsonl").write_text('{"role":"user","content":"yesterday"}\n')
config.PI_MODELS_OFFLINE = ("local/small",)
config.PERMISSION_POLICY = work / "policy.json"

RELAY = "some-relay/large-model"
LOCAL = "local/small"

print("-- the defaults are what it already did --")
for key in privacy.SWITCHES:
    check(f"{key} ships on", privacy.on(key), "the behaviour before this existed")
check("so the facts go into a remote prompt", privacy.shares("memory", RELAY))
check("and a file may be read", privacy.shares("files", RELAY))
check("and a picture attached", privacy.shares("screens", RELAY))
check("and a relay counts as remote", privacy.model_is_remote(RELAY))
check("while the local model does not", privacy.model_is_remote(LOCAL) is False)
check("an unknown model is treated as remote, not as local",
      privacy.model_is_remote(None) and privacy.model_is_remote(""))

print("-- the facts in the prompt, which is where memory actually goes --")
memory = Memory()
memory.set("router_location", "the router is in the hall")
memory.set("bank", "account 1234 at the corner bank")
prompt = PiBrain._system_prompt()
check("with the switch on, the facts are in the prompt", "corner bank" in prompt)
privacy.set_switch("memory", False)
prompt_off = PiBrain._system_prompt()
check("switched off for a remote model, the facts are not in the prompt",
      "corner bank" not in prompt_off and "router" not in prompt_off)
check("and the prompt still tells it how to store one",
      "memory_store" in prompt_off, "the mechanism is not the privacy problem")
privacy.set_switch("memory", True)
check("back on, they are back", "corner bank" in PiBrain._system_prompt())

print("-- the same switch, for the local model, changes nothing --")
# the fact never leaves the machine either way, so blocking it locally would only
# make the assistant worse at nothing
config.PRIVACY_SHARE_MEMORY = False
check("a local model still gets the facts", privacy.shares("memory", LOCAL))
check("and the prompt for a local model keeps them",
      "corner bank" in PiBrain._system_prompt(LOCAL),
      "the switch is about online models, so the local one is untouched")
check("while the prompt for a relay does not",
      "corner bank" not in PiBrain._system_prompt(RELAY))
config.PRIVACY_SHARE_MEMORY = True

print("-- no online AI at all --")
config.PRIVACY_ONLINE_AI = False
check("nothing is shared with a remote model then",
      not any(privacy.shares(kind, RELAY) for kind in ("memory", "files", "screens", "history")))
check("the local model is unaffected", privacy.shares("memory", LOCAL))
check("and the chain cannot rebuild itself onto a relay",
      all(model in config.PI_MODELS_OFFLINE for model in brain_mod.model_chain()))
check("in auto mode, with the internet up", config.PI_CHAIN_MODE == "auto",
      "this is the case that would otherwise creep back online")
config.PRIVACY_ONLINE_AI = True
check("switched back on, the chain offers the relays again",
      any(model not in config.PI_MODELS_OFFLINE for model in brain_mod.model_chain()))

print("-- a file, judged by the gate rather than by the prompt --")
policy = {
    "version": 1, "enabled": True, "roots": [str(work_root)],
    "readonly_commands": [], "approvals": str(work / "approvals.json"),
    "pending": str(work / "pending.json"), "log": str(work / "log.jsonl"),
    "approval_ttl_s": 300,
}


class Gate:
    def __init__(self):
        self.proc = subprocess.Popen(["node", str(HARNESS)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True, cwd=str(REPO))

    def ask(self, **request):
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def decide(self, tool, tool_input, pol):
        pol = {"enabled": True, "roots": [str(work_root)], "readonly_commands": [],
               "approvals": str(work / "approvals.json"), "pending": str(work / "pending.json"),
               "log": str(work / "log.jsonl"), "approval_ttl_s": 300, **pol}
        return self.ask(mode="decide", toolName=tool, input=tool_input, policy=pol,
                        cwd=str(work_root))


gate = Gate()
allowed = gate.decide("read", {"path": str(inside)}, {})
check("with nothing restricted, a file in an approved directory is readable",
      allowed.get("allow") is True)

remote_no_files = {"privacy_remote": True, "privacy_allow_files": False}
result = gate.decide("read", {"path": str(inside)}, remote_no_files)
check("a remote model with files switched off cannot read it",
      result.get("allow") is False, result.get("reason", ""))
check("and the reason is the privacy setting, not the directory",
      "privacy" in (result.get("reason") or "").lower()
      or "switched off" in (result.get("reason") or ""), result.get("reason", ""))
check("and no approval is offered, because this is not a one-off question",
      result.get("approval") is False)
check("writing is refused too",
      gate.decide("write", {"path": str(work_root / "x.txt"), "content": "x"},
                  remote_no_files).get("allow") is False)
check("and editing",
      gate.decide("edit", {"path": str(inside), "edits": [{"oldText": "a", "newText": "b"}]},
                  remote_no_files).get("allow") is False)
check("while the local model may still read it",
      gate.decide("read", {"path": str(inside)},
                  {"privacy_remote": False, "privacy_allow_files": False}).get("allow") is True)
check("and a remote model with files allowed may read it",
      gate.decide("read", {"path": str(inside)},
                  {"privacy_remote": True, "privacy_allow_files": True}).get("allow") is True)
check("a command that only looks is unaffected, since it sends no file contents",
      gate.decide("bash", {"command": "ls"}, remote_no_files).get("allow") is True)
check("the refusals are written down",
      "privacy" in (work / "log.jsonl").read_text().lower(),
      "a blocked file should be visible in the record")

print("-- the picture that would have been attached --")
captured = []


def fake_capture():
    captured.append(1)
    return b"not really a jpeg"


real_capture = assistant_mod.capture
assistant_mod.capture = fake_capture


class Silence:
    def __init__(self):
        self.spoken = []
        self.log_lines = []
        self.asked = []
        self.speaker = self
        self.brain = self
        self.player = type("P", (), {"wait_drained": lambda s, *a, **k: None})()
        self.hud = None
        self.mic_enabled = True
        self._speaking_since = 0.0
        self.turn_latency = 0.0
        self._cancel = None
        self._tasks = set()
        self._hang_count = 0
        self._privacy_confirm = privacy.Confirmation()

    _theme_intent = staticmethod(Assistant._theme_intent)
    _approval_question = staticmethod(Assistant._approval_question)

    def model_name(self):
        return self._model

    def ask(self, prompt, images=None):
        self.asked.append((prompt, images))

        async def stream():
            yield "done", "answered"
        return stream()

    def say_stream(self, deltas, on_first, cancel):
        for item in deltas:
            self.spoken.append(item)

    def say(self, text):
        self.spoken.append(text)
        return True

    def publish(self, event):
        pass

    def log(self, who, text):
        self.log_lines.append(text)

    def set_state(self, state):
        pass

    def _back_to_idle(self):
        pass

    def _publish_heard(self, text, partial=False):
        pass

    async def _restart_brain(self):
        pass


async def turn(text, model):
    voice = Silence()
    voice._model = model
    await Assistant._process(voice, text=text)
    return voice


config.PERMISSION_PENDING = work / "pending.json"
config.PERMISSIONS = False                      # not this group's subject
config.PRIVACY_SHARE_SCREENS = False
voice = asyncio.run(turn("what is on my screen", RELAY))
check("a screen question with screens switched off sends no request at all",
      voice.asked == [], f"requests made: {len(voice.asked)}")
check("and the capture is not even taken", captured == [],
      "not taking it is the honest version of not sending it")
check("and it says the picture was not sent",
      "screen" in (voice.spoken[-1] or "").lower(), voice.spoken[-1] if voice.spoken else "")

config.PRIVACY_SHARE_SCREENS = True
captured.clear()
voice = asyncio.run(turn("what is on my screen", RELAY))
check("switched back on, the picture is attached", voice.asked and voice.asked[-1][1] is not None)

config.PRIVACY_SHARE_SCREENS = False
voice = asyncio.run(turn("what is on my screen", LOCAL))
check("and a local model still gets it, because nothing leaves the machine",
      captured and voice.asked and voice.asked[-1][1] is not None)
config.PRIVACY_SHARE_SCREENS = True
assistant_mod.capture = real_capture

print("-- the conversation it resumes --")
class FakeProc:
    seen = None

    def __init__(self, args, **kwargs):
        FakeProc.seen = args
        self.stdout = io.StringIO("")          # so the reader threads end quietly
        self.stderr = io.StringIO("")
        self.stdin = io.StringIO()

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


real_models = config.PI_MODELS
config.PI_MODELS = (RELAY,)
real_popen = brain_mod.subprocess.Popen
brain_mod.subprocess.Popen = FakeProc
try:
    # the switch governs online models, so the chain has to be a relay for it to do
    # anything: making a local model forget yesterday would lose context for nothing,
    # since nothing leaves the machine either way
    for model, share, expected in ((RELAY, True, True), (RELAY, False, False),
                                   (LOCAL, False, True)):
        config.PRIVACY_SHARE_HISTORY = share
        config.PI_CONTINUE = True
        config.PI_MODELS_OFFLINE = (LOCAL,)
        config.PI_CHAIN_MODE = "offline" if model == LOCAL else "online"
        brain = PiBrain()
        asyncio.run(brain.start())
        args = FakeProc.seen
        where = "local" if model == LOCAL else "relay"
        check(f"{where}, history {'on' if share else 'off'}: --continue is "
              f"{'on' if expected else 'off'} the command line",
              ("--continue" in args) is expected, str(args[-4:]))
        if expected is not ("--continue" in args):
            print("    reason:", args[-6:])
finally:
    brain_mod.subprocess.Popen = real_popen
config.PRIVACY_SHARE_HISTORY = True
config.PI_CONTINUE = True
config.PI_MODELS = real_models

print("-- transcripts on disk --")
check("they are kept by default", privacy.on("transcripts"))
config.PRIVACY_KEEP_TRANSCRIPTS = False
check("switched off, the directory pi is given is not the saved one",
      PiBrain.session_dir_for_run() != config.SESSION_DIR,
      str(PiBrain.session_dir_for_run()))
check("and it is under the system's temporary directory, not the repo",
      str(PiBrain.session_dir_for_run()).startswith(tempfile.gettempdir()))
config.PRIVACY_KEEP_TRANSCRIPTS = True
check("switched back on, it is the saved one again",
      PiBrain.session_dir_for_run() == config.SESSION_DIR)

print("-- switching one off, from the assistant's point of view --")
# the setting has to survive a restart, so it is written where personal settings live
local_config = work / "config.local.json"
real_local = config.LOCAL_CONFIG_FILE
config.LOCAL_CONFIG_FILE = local_config
written = privacy.set_switch("memory", False)
check("the choice is written down", written == local_config)
check("into the file personal settings live in", local_config.exists())
check("as the name config.py reads", json.loads(local_config.read_text())["PRIVACY_SHARE_MEMORY"] is False)
check("while config.py still holds the default unchanged",
      "PRIVACY_SHARE_MEMORY = True" in (
          REPO / "src" / "archy" / "config.py").read_text(),
      "the defaults stay in config.py, the choice lives beside it")
check("a setting that is not one is refused, not invented",
      _raises_keyerror(privacy.set_switch, "telepathy", False))
local_config.write_text("{broken")
check("a config file that will not parse does not stop the switch being usable",
      privacy.set_switch("memory", True) == local_config
      and json.loads(local_config.read_text())["PRIVACY_SHARE_MEMORY"] is True)
config.LOCAL_CONFIG_FILE = real_local
privacy.set_switch("memory", True)

for key, raw in (("memory", "off"), ("files", 0), ("screens", "false"), ("history", "no"),
                 ("memory", "on"), ("files", 1), ("screens", True)):
    name, _ = privacy.SWITCHES[key] if key != "memory" or raw != "on" else ("PRIVACY_SHARE_MEMORY", True)
    wanted = str(raw).strip().lower() not in ("0", "false", "no", "off") if not isinstance(raw, (int, float)) \
        else raw != 0
    setattr(config, name, raw)
    check(f"a {type(raw).__name__} ({raw!r}) in the config file is read as "
          f"{'on' if wanted else 'off'}", privacy.on(key) is wanted, f"{name} = {raw!r}")
    setattr(config, name, True)
setattr(config, "PRIVACY_SHARE_MEMORY", "maybe")
check("an unusable value falls back to the default rather than guessing",
      privacy.on("memory") is True, "and says so once, rather than every turn")
config.PRIVACY_SHARE_MEMORY = True

print("-- forgetting, which is the one thing a switch cannot undo --")

memory = Memory()
memory.set("zz-first", "zz-secret-one")
memory.set("zz-second", "zz-secret-two")
check("there is something to forget", privacy.memory_facts() >= 2, str(privacy.memory_facts()))
result = privacy.clear_memory()
check("clearing removes the facts", privacy.memory_facts() == 0, str(result))
check("and keeps a copy beside them", Path(result["backup"]).exists(), str(result["backup"]))
check("which holds what was there", "zz-secret-one" in Path(result["backup"]).read_text())
check("and the prompt no longer has them",
      "zz-secret" not in PiBrain._system_prompt())

check("the saved conversations are findable", len(privacy.history_files()) == 1)
result = privacy.clear_history()
check("clearing removes them", privacy.history_files() == [], str(result))
check("and keeps a copy of those too", Path(result["backup"]).exists())
check("without touching the memory file",
      config.MEMORY_FILE.exists(), "two different things, cleared separately")

print("-- forgetting by voice, which takes two sentences --")
confirm = privacy.Confirmation()
memory.set("c", "three")
check("the request is recognised", privacy.asked_to_clear("forget everything you know about me"))
check("and it is not the confirmation", privacy.confirmed_clear("forget everything") is False)
check("a question is not a request", privacy.asked_to_clear("should I forget everything?") is False)
check("nor is talking about it", privacy.asked_to_clear(
    "I told my friend to forget everything he knew") is False)
said = confirm.ask()
check("asking says how much it would delete", "2" in said or "1" in said, said)
check("and how to confirm it", "yes, forget everything" in said.lower(), said)
check("an unrelated yes does not confirm", confirm.confirm("yes") is None)
check("and it is still waiting", confirm.waiting())
check("nor does a yes about something else",
      confirm.confirm("yes and also delete my files while you are there") is None)
check("the phrase it asked for does confirm",
      confirm.confirm("yes, forget everything") is not None)
check("and then it is not waiting any more", confirm.waiting() is False)

expired = privacy.Confirmation(window_s=0.0)
expired.ask()
time.sleep(0.01)
check("a confirmation asked for long ago does nothing",
      expired.confirm("yes, forget everything") is None,
      "a yes is about the moment it was asked")

print("-- what the dashboard will read --")
state = privacy.status()
check("every switch is in the state", set(state["switches"]) == set(privacy.SWITCHES))
check("and it is not a claim of enforcement it cannot make",
      state["enforced"] is True, "these are checked in code")
check("the spoken form is honest when nothing is restricted",
      "Nothing is restricted" in privacy.spoken_status())
config.PRIVACY_SHARE_FILES = False
check("and names what is off", "file contents" in privacy.spoken_status())
config.PRIVACY_SHARE_FILES = True

print("-- and by voice, through the assistant --")
memory.set("d", "four")


async def speak_to(voice, text):
    await Assistant._process(voice, text=text)
    return voice


voice = Silence()
voice._model = RELAY
asyncio.run(speak_to(voice, "forget everything you know about me"))
check("the request is answered with what it would delete",
      "cannot be undone" in (voice.spoken[-1] or "").lower(), voice.spoken[-1] if voice.spoken else "")
check("and nothing is deleted yet", privacy.memory_facts() >= 1)
asyncio.run(speak_to(voice, "yes, forget everything"))
check("the confirmation deletes it", privacy.memory_facts() == 0, str(privacy.memory_facts()))
check("and says so", "deleted" in (voice.spoken[-1] or "").lower()
      or "forgot" in (voice.spoken[-1] or "").lower(), voice.spoken[-1] if voice.spoken else "")
check("nothing left to delete is not an error", privacy.memory_facts() == 0)

asyncio.run(speak_to(voice, "what are you allowed to send"))
check("asking what it may send is answered locally",
      "online models" in (voice.spoken[-1] or "").lower()
      or "nothing is restricted" in (voice.spoken[-1] or "").lower(),
      voice.spoken[-1] if voice.spoken else "")

for name, value in saved.items():
    setattr(config, name, value)
print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}  ({passed} checks)")
sys.exit(1 if failed else 0)
