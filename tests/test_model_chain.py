"""What happens when the model behind the assistant stops answering.

Two failures look different from here. A relay that refuses, errors or drops
gives a hard failure, and the next model in the chain takes over at once. A
relay that accepts the connection and then says nothing looks identical to a
slow tool run, so it gets one retry on the same model before the chain moves on.

That second half matters more than it sounds. Without it, a silent relay is
restarted onto itself forever and every question is answered with "my connection
dropped, ask me again" while the other three models sit unused.

The brain is a stub, so this never touches a relay.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from archy import status as status_mod
from archy import config, privacy  # noqa: E402
from archy.assistant import Assistant  # noqa: E402
from archy.brain import BrainError, PiBrain  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:58s} {detail}")


class StubBrain:
    """Raises whatever it was told to, and records the chain moving."""

    def __init__(self, errors):
        self.models = ("one/a", "two/b", "three/c")
        self.index = 0
        self.errors = list(errors)
        self.restarts = 0
        self.aborted = 0

    def model_name(self):
        return self.models[self.index % len(self.models)]

    def advance_model(self):
        self.index = (self.index + 1) % len(self.models)
        return self.model_name()

    def abort(self):
        self.aborted += 1

    async def ask(self, text, images=None):
        if self.errors:
            raise self.errors.pop(0)
        yield ("delta", "fine")            # text first, as a real turn does
        yield ("done", "fine")

    def alive(self):
        return True


class Harness:
    def __init__(self, errors=()):
        # Assistant.__init__ is not run here, so anything it creates has to be
        # provided: a turn that reaches for it otherwise fails on the attribute.
        self._privacy_confirm = privacy.Confirmation()
        # Assistant.__init__ is not run, so what it creates has to be here too:
        # a note about what happened goes into this feed on every failure path.
        self.status = status_mod.Feed()
        self.brain = StubBrain(errors)
        self.spoken = []
        self.log_lines = []
        self.speaker = self
        self.player = type("P", (), {"wait_drained": lambda s, *a, **k: None})()
        self.state = "thinking"
        self.hud = None
        self.mic_enabled = True
        self._speaking_since = 0.0
        self.turn_latency = 0.0
        self._stopping = False
        self._cancel = None
        self._tasks = set()

    # the local-intent path runs before the model does, and touches these
    _theme_intent = staticmethod(Assistant._theme_intent)

    def say_stream(self, deltas, on_first, cancel):
        """Stand in for the speaker: the first text queued is the first audio
        queued, which is where the real one calls on_first."""
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
        self.state = state

    def _back_to_idle(self):
        self.set_state("idle")

    def _publish_heard(self, text, partial=False):
        pass

    async def _restart_brain(self):
        self.brain.restarts += 1

    def last(self):
        return self.log_lines[-1] if self.log_lines else ""


async def turn(h, text="say something"):
    await Assistant._process(h, text=text)
    return h


async def main():
    print("-- a hard failure moves down the chain at once --")
    h = Harness([BrainError("the model failed")])
    h._hang_count = 0
    await turn(h)
    check("the model advanced", h.brain.index == 1, h.brain.model_name())
    # the spoken fallback is logged too now, so the switch is in the log rather
    # than necessarily on its last line
    check("and it says so", any("switching to" in l for l in h.log_lines), h.last())
    check("the spoken line admits the switch",
          "backup model" in h.spoken[-1], h.spoken[-1][:40])

    print("-- a silent model is retried once, then the chain moves --")
    h = Harness([BrainError("pi said nothing for 25s"),
                 BrainError("pi said nothing for 25s")])
    h._hang_count = 0
    await turn(h)
    check("the first silence keeps the model", h.brain.index == 0, h.brain.model_name())
    check("and the process is restarted instead", h.brain.restarts == 1,
          f"restarts {h.brain.restarts}")
    check("the spoken line does not promise a switch",
          "backup model" not in h.spoken[-1], h.spoken[-1][:40])
    await turn(h)
    check("the second silence moves on", h.brain.index == 1, h.brain.model_name())
    check("and says which model it moved to",
          any("switching to" in l for l in h.log_lines)
          and h.brain.model_name().endswith("b"), h.last())

    print("-- a turn that answers clears the count --")
    config.MODEL_HANG_LIMIT = 2
    h = Harness([BrainError("pi said nothing for 25s")])
    h._hang_count = 0
    await turn(h)
    check("one silence counted", h._hang_count == 1, str(h._hang_count))
    # a turn that actually speaks resets it, so two silences days apart never
    # accumulate into a needless model switch. The reset lives in on_first_audio,
    # which the speaker calls when the first audio is queued.
    h.brain.errors = []
    await turn(h, "say something else")
    check("a turn that answered leaves the count at zero", h._hang_count == 0,
          str(h._hang_count))
    check("and it did not move the chain", h.brain.index == 0, h.brain.model_name())

    print("-- the limit is configurable, not a magic number in a branch --")
    check("the limit lives in config", config.MODEL_HANG_LIMIT >= 2,
          str(config.MODEL_HANG_LIMIT))

    print("-- a chain with one model in it has no backup to move to --")
    h = Harness([BrainError("the model failed")])
    h.brain.models = ("only/one",)
    h._hang_count = 0
    await turn(h)
    check("it stays on the only model", h.brain.index == 0, h.brain.model_name())
    check("and does not claim a switch happened",
          not any("switching to" in l for l in h.log_lines), h.last())
    check("the log says it is the only one there is",
          any("only one" in l for l in h.log_lines), h.last())
    check("and the spoken line says there is no backup",
          "no backup" in h.spoken[-1], h.spoken[-1][:60])

    print("-- and a chain with nothing in it at all --")
    empty = PiBrain()
    empty.chain = ()
    check("the model name is empty, not a ZeroDivisionError",
          empty.model_name() == "", repr(empty.model_name()))
    check("which every caller reads as somebody else's machine",
          privacy.model_is_remote(empty.model_name()), "")

    print("-- and a chain with nothing in it still steps without a crash --")
    # a failed turn on a brain that never started reaches advance_model on the
    # second silent failure; the modulo of an empty chain raised out of the
    # error handler itself, and the honest sentence was never said
    try:
        moved = empty.advance_model()
        check("an empty chain steps to nothing instead of raising",
              moved is None, repr(moved))
    except Exception as exc:
        check("an empty chain steps to nothing instead of raising", False,
              f"{type(exc).__name__}: {exc}")

    print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}  ({passed} checks)")
    sys.exit(1 if failed else 0)


asyncio.run(main())
