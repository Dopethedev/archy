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
import jarvis.assistant as assistant_mod  # noqa: E402
from jarvis import status as status_mod
from jarvis import config, net, privacy  # noqa: E402
from jarvis.assistant import Assistant  # noqa: E402
from jarvis.brain import BrainError  # noqa: E402
from jarvis.stt import DeafTranscriber  # noqa: E402
from jarvis.tts import MuteSpeaker  # noqa: E402

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
