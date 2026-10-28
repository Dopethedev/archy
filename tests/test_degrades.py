"""What the assistant does when something is missing or the network is gone.

Two failures used to be indistinguishable from the outside. One missing model file
ended the process in the constructor, before the microphone was ever opened, so
there was no assistant, no dashboard and no console beyond a traceback. And every
model problem was spoken as "my connection dropped", including the ones where the
connection was perfectly fine and the model name was simply wrong.

Nothing here touches a relay, a microphone or a model file: the brain is a stub,
the parts that load files are replaced with ones that raise on purpose, and the
connectivity verdicts come from strings captured off the real thing.
"""
import asyncio
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import archy.assistant as assistant_mod  # noqa: E402
from archy import status as status_mod
from archy import config, net, privacy  # noqa: E402
from archy.assistant import Assistant  # noqa: E402
from archy import audio as audio_mod  # noqa: E402
from archy.brain import BrainError  # noqa: E402
from archy.stt import DeafTranscriber  # noqa: E402
from archy.tts import MuteSpeaker  # noqa: E402

# No test starts a model server: it is a real process holding real VRAM, and the
# behaviour under test is what the assistant does with its answer.
started = []
real_ensure = assistant_mod.local_model.ensure_running
assistant_mod.local_model.ensure_running = lambda *a, **k: started.append(1) or True

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:58s} {detail}")


def raising(what):
    def build(*a, **k):
        raise RuntimeError(what)
    return build


print("-- a missing model file does not take the assistant with it --")
real_transcriber, real_speaker = assistant_mod.Transcriber, assistant_mod.Speaker
real_mic, real_player = assistant_mod.MicCapture, assistant_mod.Player


class Stub:
    def __init__(self, *a, **k):
        self.drained = 0

    def drain(self):
        self.drained += 1

    def start(self):
        pass

    def stop(self):
        pass


assistant_mod.Transcriber = raising("the whisper model is not here")
assistant_mod.Speaker = raising("no usable speech engine")
assistant_mod.MicCapture = assistant_mod.Player = Stub
try:
    a = Assistant()
    check("the constructor survives", True)
    check("it hears nothing instead of dying",
          isinstance(a.transcriber, DeafTranscriber), type(a.transcriber).__name__)
    check("it speaks nothing instead of dying",
          isinstance(a.speaker, MuteSpeaker), type(a.speaker).__name__)
    check("and the reason is carried, not swallowed",
          "whisper" in str(a.transcriber.error)
          and "speech engine" in str(a.speaker.error))
finally:
    (assistant_mod.Transcriber, assistant_mod.Speaker) = (real_transcriber, real_speaker)
    (assistant_mod.MicCapture, assistant_mod.Player) = (real_mic, real_player)

print("-- the stand-ins are honest rather than quietly broken --")
mute = MuteSpeaker(RuntimeError("no engine"))
check("a line that was never spoken returns False",
      mute.say("hello there") is False, "the reminder loop reads this as undelivered")
check("and it is kept for the record", mute.spoken == ["hello there"], str(mute.spoken))
check("synthesis returns no audio", mute.synth("hi").size == 0)
spoke = []
stream = iter(["one ", "two", None])
consumed = []
while True:
    item = next(stream, "STOP")
    if item is None:
        break
    consumed.append(item)
mute.say_stream(iter(consumed), on_spoken=lambda: spoke.append(1))
check("streaming drains the text instead of hanging the turn",
      mute.spoken[-1] == "one two", str(mute.spoken[-1]))
check("and it never claims to have started speaking", spoke == [])

deaf = DeafTranscriber(RuntimeError("no model"))
check("a deaf transcriber hears nothing", deaf.transcribe(np.zeros(16000)) == "")

print("-- a microphone that will not open does not take the assistant with it --")
# A device name that matches nothing raised out of MicCapture's constructor, and
# that constructor is reached from Assistant's, which is reached from main()
# outside every try: one stale INPUT_DEVICE in config.local.json, or one mistyped
# --input, ended the run with a traceback before the dashboard was even built.
build = getattr(assistant_mod, "make_mic", None)


def refuses(mic) -> bool:
    """Opening it reports the reason instead of ending the run."""
    try:
        mic.start()
    except Exception as exc:
        return str(getattr(mic, "error", "")) in str(exc)
    return False


def pump_safe(mic) -> bool:
    """Everything the microphone pump touches, on a microphone with no device."""
    try:
        mic.set_focus(True)
        mic.set_focus(False)
        mic.drain()
        float(mic.silent_seconds())
        float(mic.level)
        mic.frames.put_nowait(np.zeros(16, np.int16))
        mic.drain()
        mic.stop()
    except Exception as exc:
        print(f"    the pump broke on it: {type(exc).__name__}: {exc}")
        return False
    return True


check("the microphone is built through something that survives a bad name",
      callable(build), type(build).__name__)
if callable(build):
    stand_in = build("no such device on this machine at all")
    check("a name that matches nothing gives a stand-in, not an exception",
          isinstance(stand_in, audio_mod.DeafMic), type(stand_in).__name__)
    check("and it carries the reason",
          "no such device" in str(getattr(stand_in, "error", "")),
          str(getattr(stand_in, "error", ""))[:60])
    check("and opening it reports rather than raising out of the run",
          refuses(stand_in), "start() hands the reason to the startup path")
    check("and every call the pump makes is safe on it", pump_safe(stand_in))
    check("a name that does match still gets a real microphone",
          isinstance(build(None), audio_mod.MicCapture), type(build(None)).__name__)

print("-- a task that fails is named, not collected --")


class Mini(Assistant):
    """Enough assistant to spawn a task on, with the collaborators stubbed."""

    def __init__(self):
        self.lines = []
        self.states = []
        self.said = []
        self._tasks = set()
        self._stopping = False
        self.mic_enabled = True
        self.hud = None
        self.state = "idle"
        self.mic = type("M", (), {"drain": lambda s: None})()
        self.player = type("P", (), {
            "write": lambda s, *a, **k: True,
            "wait_drained": lambda s, *a, **k: None,
            "start_reply": lambda s: 1})()
        self.speaker = type("S", (), {
            "say": lambda s, text: self.said.append(text) or True})()

    def log(self, who, text):
        self.lines.append(f"{who}: {text}")

    def set_state(self, state):
        self.states.append(state)

    def _back_to_idle(self):
        self.set_state("idle")


async def spawn_a_failure(mini):
    async def dies():
        raise RuntimeError("the compositor exploded")

    mini._spawn(dies())
    await asyncio.sleep(0.05)


mini = Mini()
asyncio.run(spawn_a_failure(mini))
check("a task that died is named in the log",
      any("the compositor exploded" in line for line in mini.lines), str(mini.lines))
check("with what it was, not only that something happened",
      any("RuntimeError" in line for line in mini.lines), "a bare 'it failed' names nothing")
check("and it is printed, so a run with no dashboard still shows it",
      any("the turn failed" in line for line in mini.lines), str(mini.lines)[:80])
check("and a turn that died says something",
      "Something went wrong there." in mini.said, str(mini.said))
check("and gives the state back instead of holding it",
      (mini.states[-1] if mini.states else None) == "idle", str(mini.states))

clean = Mini()


async def spawn_a_success(mini_ok):
    async def fine():
        return "done"

    mini_ok._spawn(fine())
    await asyncio.sleep(0.05)


asyncio.run(spawn_a_success(clean))
check("a task that worked is not reported as a failure",
      clean.lines == [] and clean.said == [] and clean.states == [],
      str(clean.lines + clean.said + clean.states))
check("and the held reference is released either way",
      clean._tasks == set(), str(clean._tasks))

print("-- a reminder that cannot be spoken does not end the reminder loop --")
# The label is removed from the store as it is returned, so an undelivered
# reminder is put back for a retry. That write raising (a state directory that is
# not writable, a full disk) escaped the loop and ended it: every reminder after
# that one was silently never spoken, for the life of the process.
reminder = Mini()
real_due, real_add = assistant_mod.reminders_mod.due_items, assistant_mod.reminders_mod.add
real_check_s = config.REMINDER_CHECK_S
calls = {"due": 0}


def one_due(now=None):
    calls["due"] += 1
    return [{"label": "check the oven", "due": 0.0}] if calls["due"] == 1 else []


def cannot_write(*a, **k):
    raise OSError("no space left on the device")


assistant_mod.reminders_mod.due_items = one_due
assistant_mod.reminders_mod.add = cannot_write
config.REMINDER_CHECK_S = 0.02
reminder.speaker = type("S", (), {"say": lambda s, text: False})()


try:
    async def run_the_loop(mini_loop):
        task = asyncio.ensure_future(mini_loop._reminder_loop())
        await asyncio.sleep(0.3)
        alive = not task.done()
        mini_loop._stopping = True
        task.cancel()
        return alive

    alive = asyncio.run(run_the_loop(reminder))
finally:
    assistant_mod.reminders_mod.due_items, assistant_mod.reminders_mod.add = real_due, real_add
    config.REMINDER_CHECK_S = real_check_s

check("the loop is still running after a reminder could not be put back",
      alive, "it ended, so no later reminder would ever be spoken")
check("it kept checking rather than stopping at the failure",
      calls["due"] > 1, f"{calls['due']} checks")
check("and the reason is in the log",
      any("could not be put back" in line for line in reminder.lines),
      str(reminder.lines)[:90])

print("-- starting one part that fails leaves the rest running --")


class Starter(Assistant):
    """Just enough assistant to call _start on."""

    def __init__(self):
        self.lines = []

    def log(self, who, text):
        self.lines.append(text)


async def start_checks():
    s = Starter()
    check("a good step reports success", await s._start("fine", lambda: None) is True)
    check("a sync step that raises is caught", await s._start("bad", raising("boom")) is False)
    check("and it is reported, not swallowed", any("boom" in l for l in s.lines),
          s.lines[-1] if s.lines else "")

    async def async_boom():
        raise OSError("no device")

    check("an async step that raises is caught",
          await s._start("mic", async_boom) is False)
    check("with the reason logged", any("no device" in l for l in s.lines),
          s.lines[-1] if s.lines else "")

    async def async_ok():
        return 1

    check("an async step that works still works", await s._start("fine", async_ok) is True)

    print("-- a missing speech detector is reported once, not every frame --")
    attempts = []
    real_vad = assistant_mod.SpeechVAD
    assistant_mod.SpeechVAD = lambda *a, **k: attempts.append(1) or raising("no silero")()
    try:
        s._vad = None
        s._vad_missing = False
        frame = np.zeros(480, dtype=np.int16)
        first = s.speech_score(frame)
        second = s.speech_score(frame)
        check("silence is what it reports", first == 0.0 and second == 0.0, f"{first} {second}")
        check("and it only looks for the model once", len(attempts) == 1, str(len(attempts)))
        check("saying why exactly once",
              sum("speech detector" in l for l in s.lines) == 1,
              str(s.lines))
    finally:
        assistant_mod.SpeechVAD = real_vad


asyncio.run(start_checks())

print("-- a chain failure is spoken as what it actually was --")


class StubBrain:
    def __init__(self, errors):
        self.models = ("one/a", "two/b")
        self.index = 0
        self.errors = list(errors)
        self.switched = 0
        self.chain = self.models

    def model_name(self):
        return self.chain[self.index % len(self.chain)]

    def advance_model(self):
        self.index = (self.index + 1) % len(self.chain)
        return self.model_name()

    def switch_chain(self):
        # the real one recomputes the chain from the network; this records that
        # the assistant asked for that instead of walking four dead relays
        self.switched += 1
        self.index = 0
        return self.model_name()

    def on_local_model(self):
        return self.model_name() in config.PI_MODELS_OFFLINE

    def abort(self):
        pass

    async def ask(self, text, images=None):
        if self.errors:
            raise self.errors.pop(0)
        yield ("delta", "fine")
        yield ("done", "fine")

    def alive(self):
        return True


class Harness:
    def __init__(self, errors=()):
        # Assistant.__init__ is not run, so what it creates has to be here: the
        # confirmation object is reached for on every turn, before anything else.
        self._privacy_confirm = privacy.Confirmation()
        # Assistant.__init__ is not run, so what it creates has to be here too:
        # a note about what happened goes into this feed on every failure path.
        self.status = status_mod.Feed()
        self.brain = StubBrain(errors)
        self.spoken = []
        self.log_lines = []
        self.speaker = self
        self.player = type("P", (), {"wait_drained": lambda s, *a, **k: None})()
        self.hud = None
        self.mic_enabled = True
        self._speaking_since = 0.0
        self.turn_latency = 0.0
        self._cancel = None
        self._tasks = set()

    _theme_intent = staticmethod(Assistant._theme_intent)

    def say_stream(self, deltas, on_first, cancel):
        queued = False
        for item in deltas:
            self.spoken.append(item)
            if not queued:
                queued = True
                on_first()

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


async def turn(h):
    await Assistant._process(h, text="what is the weather")
    return h.spoken[-1] if h.spoken else ""


h = Harness([BrainError("getaddrinfo ENOTFOUND relay.example")])
h._hang_count = 0
said = asyncio.run(turn(h))
check("no internet is spoken as no internet", "no internet" in said.lower(), said)
check("and not as a dropped connection", "connection" not in said.lower(), said)
check("the verdict is recorded for the dashboard",
      net.net.mode() == net.OFFLINE, net.net.mode())
check("so the local chain is what comes next", net.net.likely_online() is False)

saved = config.PI_MODELS_OFFLINE
config.PI_MODELS_OFFLINE = ("llama/local",)
h = Harness([BrainError("fetch failed")])
h._hang_count = 0
started.clear()
said = asyncio.run(turn(h))
check("with a local model configured it says which one answered",
      "local model" in said, said)
check("the chain is rebuilt instead of walked",
      h.brain.switched == 1, str(h.brain.switched))
check("and the local server is started before the brain restarts",
      len(started) == 1, str(len(started)))

# the server that will not start is the honest failure: it must not promise a
# local model that is not there
assistant_mod.local_model.ensure_running = lambda *a, **k: False
h = Harness([BrainError("getaddrinfo ENOTFOUND relay.example")])
h._hang_count = 0
said = asyncio.run(turn(h))
check("a local model that will not start is said so",
      "not running" in said, said)
assistant_mod.local_model.ensure_running = real_ensure
config.PI_MODELS_OFFLINE = saved

print("-- but a refusal from the provider proves the network is up --")
check("the last verdict was offline", net.net.internet() is False)
h = Harness([BrainError("401 Unauthorized: invalid api key")])
h._hang_count = 0
said = asyncio.run(turn(h))
check("a refused key is not reported as an outage", "key" in said, said)
check("and the internet is now known to be up",
      net.net.internet() is True,
      "a 401 that came back is proof the packets went out")
check("with the provider marked down", net.net.provider() is False)
check("so it is degraded, not offline", net.net.mode() == net.DEGRADED, net.net.mode())
check("and the online chain is still the right one", net.net.likely_online() is True)

print("-- a turn that answers is proof the network is fine --")
net.net.note_failure("ENOTFOUND relay.example")
check("start from offline", net.net.likely_online() is False)
h = Harness([])
h._hang_count = 0
asyncio.run(turn(h))
check("a real answer flips it back", net.net.internet() is True, str(net.net.internet()))
check("and the mode agrees", net.net.mode() == net.ONLINE, net.net.mode())

print()
print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
sys.exit(0 if not failed else 1)
