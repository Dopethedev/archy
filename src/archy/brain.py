"""The brain: one long-lived `pi --mode rpc` process.

Spawned once and kept alive, so the conversation has memory and there is no
per-request process start-up. Responses stream in token by token, which lets
speech begin before the reply is finished.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import tempfile
from pathlib import Path
import sys
import threading
import time

from . import config
from . import net
from . import permissions
from . import privacy
from .memory_store import Memory

DIALOG_METHODS = {"select", "confirm", "input", "editor"}

# What abort puts in the queue to wake the turn it is interrupting. A turn parked
# on the queue sees it and returns, which is the whole point: see PiBrain._wake.
ABORT = "__archy_abort__"

# How much can be waiting to be written to pi before the child is judged to have
# stopped reading. A prompt is a few kilobytes and pi reads it at once, so a
# backlog this deep is not a slow child, it is one that has stopped.
WRITE_QUEUE = 32


def model_chain() -> tuple[str, ...]:
    """Which models to try, in order, for the network as it looks now.

    With internet: the relays, and the local model behind them as a floor. The
    floor matters because a relay can also fail for reasons that are not the
    network (a bad key, a model retired), and a small answer beats no answer.

    Without internet: the local model alone. Trying relays that cannot be reached
    first would spend a silence deadline per question on a connection that is not
    coming back, which is the failure this whole path exists to avoid.

    Offline-only means offline-only, so with nothing local configured the chain is
    empty and the brain refuses to start rather than reaching for a relay. Handing
    back the relays made the mode a suggestion: the one setting whose whole purpose
    is to keep a question on this machine quietly did the opposite. The refusal
    names the setting to fill in.

    With no local chain configured and the mode left on auto this returns exactly
    the old tuple, so an install that never fetched a model behaves as it always
    did.
    """
    local = tuple(config.PI_MODELS_OFFLINE)
    if not privacy.on("online_ai"):
        # Switching online AI off has to mean the relays cannot come back, including
        # through the fallback that exists for the network going away. It is the one
        # privacy setting that has to hold in every branch below.
        return local or ()
    if config.PI_CHAIN_MODE == "offline":
        return local
    if config.PI_CHAIN_MODE == "online":
        return tuple(config.PI_MODELS)
    if local and not net.net.likely_online():
        return local
    return tuple(config.PI_MODELS) + local


def no_model_reason() -> str:
    """Why there is no chain to ask, in words that name the setting to change.

    Two mistakes end up here and they have different fixes, so the generic "no
    model is configured" sent half the readers to the wrong one. This text is all
    the owner has to go on when the assistant refuses to start.
    """
    if not privacy.on("online_ai"):
        return "online AI is switched off and no local model is configured"
    if config.PI_CHAIN_MODE == "offline":
        return ("PI_CHAIN_MODE is offline and PI_MODELS_OFFLINE is empty, so there "
                "is no model to ask: fill in PI_MODELS_OFFLINE in config.local.json, "
                "or set PI_CHAIN_MODE back to auto")
    return "no model is configured"


class BrainError(RuntimeError):
    pass


class PiBrain:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._stderr: list[str] = []
        # bumped on every start, so a reader thread belonging to a previous
        # process cannot inject its "process ended" marker into the new queue
        self._generation = 0
        # names the prompt being asked, so its acceptance can be told apart from
        # anything left over by the run before it
        self._request = 0
        # one turn at a time. Two turns reading one queue take each other's
        # events: the new one eats the old one's end marker and then waits out
        # its own silence deadline, which is what an interrupt used to do. The
        # lock is taken for the whole of a turn, and abort sets the event so the
        # turn holding it gives up within a loop iteration instead of waiting.
        self._turn_lock: asyncio.Lock | None = None
        self._aborted = threading.Event()
        # what the loop hands to pi, and the thread that writes it. See _write_stdin
        # for why a write is not done from the loop.
        self._writes: queue.Queue | None = None
        self._write_error = ""
        # where we are in the model chain. The free relays die without warning
        # (a model that answered at breakfast 404s by lunch), so the assistant
        # moves down the chain and says so rather than failing. The chain itself
        # is decided in start(), from whether there is internet.
        self.model_index = 0
        self.chain: tuple[str, ...] = tuple(config.PI_MODELS)

    @staticmethod
    def _system_prompt(model: str | None = None) -> str:
        """Voice rules, plus how to use the durable store, plus what is in it.

        The stored facts go into the prompt itself so that remembering costs no
        tool call and no round trip. It is capped, because a prompt that grows
        without bound would slow every turn down.

        Which is also why the model matters here: this prompt is what a request
        carries, so the facts are left out entirely when an online model is
        answering and its owner said memory may not go there. Not a sentence
        asking it to keep quiet, the text itself, absent.
        """
        python = sys.executable
        how = (
            f" You keep durable facts about {config.USER_NAME} in {config.MEMORY_FILE}. "
            f"Read them with `{python} -m archy.memory_store list`, add one with "
            f"`{python} -m archy.memory_store set NAME \"VALUE\"`, and remove one "
            "with `forget NAME`. Whenever you are asked to remember, note, or keep "
            "something, store it there, because the conversation alone will not "
            "survive. Do it quietly without announcing the mechanism.\n"
        )
        if privacy.shares("memory", model):
            try:
                memory = Memory()
                if memory.line_count():
                    facts = memory.summary()
                    if len(facts) > 1200:
                        facts = facts[:1200] + " ..."
                    how += f"\nWhat you already know about him: {facts}\n"
            except Exception as exc:
                print(f"[memory] could not load: {exc}")
        else:
            how = how.split("\n")[0]             # how to store one, not what is in it
        return config.VOICE_RULES + how

    @staticmethod
    def session_dir_for_run() -> Path:
        """Where pi keeps this run's transcripts.

        Retaining them is a privacy choice, and switching it off has to mean the
        words are not written to the repository at all, so pi is pointed at a
        temporary directory that is emptied first. Nothing else reads it, and a
        restart does not leave the last conversation behind.
        """
        if privacy.on("transcripts"):
            return config.SESSION_DIR
        directory = Path(tempfile.gettempdir()) / f"archy-transcripts-{os.getpid()}"
        try:
            if directory.exists():
                for path in directory.glob("*.jsonl"):
                    path.unlink()
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[brain] could not prepare {directory}: {exc}", flush=True)
        return directory

    # ------------------------------------------------------------------ setup
    @staticmethod
    def session_bytes() -> int:
        """How much conversation is on disk, across every session file."""
        total = 0
        try:
            for path in config.SESSION_DIR.glob("*.jsonl"):
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        except OSError:
            return 0
        return total

    @staticmethod
    def archive_sessions(budget_mb: float = config.SESSION_ARCHIVE_MB) -> int:
        """Move the conversation aside once it grows past the budget.

        pi compacts the context by itself, so the model is never handed a
        session too large for it; what grows without bound is the transcript on
        disk, and every start pays for it in load time (about a second at a
        megabyte on this machine). Facts worth keeping live in the memory store,
        which is untouched, so what is lost here is the chat history.

        The whole directory moves: pi resumes the newest file it finds in the
        session directory, so leaving older ones behind would silently resume
        the wrong conversation. The archive goes outside that directory for the
        same reason. Returns the number of files archived.
        """
        if not config.SESSION_DIR.exists():
            return 0
        files = sorted(config.SESSION_DIR.glob("*.jsonl"))
        if not files or PiBrain.session_bytes() <= budget_mb * 1024 * 1024:
            return 0
        stamp = time.strftime("%Y%m%dT%H%M%S")
        target = config.STATE_DIR / "session-archive" / stamp
        try:
            target.mkdir(parents=True, exist_ok=True)
            moved = 0
            for path in files:
                path.replace(target / path.name)
                moved += 1
            print(f"[brain] archived {moved} session file(s) into {target}", flush=True)
            return moved
        except OSError as exc:
            print(f"[brain] could not archive the session: {exc}", flush=True)
            return 0

    @staticmethod
    def _is_local(model: str) -> bool:
        return model in config.PI_MODELS_OFFLINE

    async def start(self):
        config.SESSION_DIR.mkdir(exist_ok=True)
        session_dir = self.session_dir_for_run()
        self._generation += 1
        generation = self._generation
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()

        self.chain = model_chain()
        if not self.chain:
            # There is no honest way to answer with nothing to ask, so it says why
            # instead of quietly reloading a relay and calling the setting respected.
            raise BrainError(no_model_reason())
        if self.model_index >= len(self.chain):
            self.model_index = 0
        model = self.chain[self.model_index]
        args = [
            config.PI_BIN, "--mode", "rpc",
            "--session-dir", str(session_dir),
            "--thinking", config.PI_THINKING,
            "--append-system-prompt", self._system_prompt(model),
        ]
        args += ["--model", model]
        if self._is_local(model) and config.PI_LOCAL_SYSTEM_PROMPT:
            # pi's own prompt is a coding assistant's, and on a 4B model on this
            # machine it costs 5099 tokens to read before the first word against
            # 966 for the voice one. The relay is not paying that, so only the
            # local model is handed the short one.
            args += ["--system-prompt", config.PI_LOCAL_SYSTEM_PROMPT]
        print(f"[brain] model {model}", flush=True)
        if config.PI_CONTINUE and privacy.shares("history", model):
            # With history sharing off for an online model, a fresh session is the
            # only way the earlier conversation is genuinely not sent.
            args.append("--continue")          # pick the conversation back up
        if not config.PI_EXTENSIONS:
            args.append("--no-extensions")
        # The permission gate, when the owner turned it on. It raises here rather
        # than later if the file that enforces it is not there: starting pi with
        # the promise of a gate and no gate is worse than not starting it. With
        # the gate off there is no gate, no policy and no enforcement, and the
        # policy is the rulebook a running gate reads: writing one anyway, on
        # every start, rewrote the rules for a gate that was not running.
        if permissions.enabled():
            permissions.note_model(model)
            permissions.write_policy()
        args += permissions.gate_args()

        environment = dict(os.environ, **permissions.gate_env())
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
        )
        self._writes = queue.Queue(maxsize=WRITE_QUEUE)
        self._write_error = ""
        threading.Thread(target=self._write_stdin, args=(generation, self._proc),
                         daemon=True).start()
        threading.Thread(target=self._read_stdout, args=(generation, self._proc),
                         daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(generation, self._proc),
                         daemon=True).start()

    def _write_stdin(self, generation: int, proc):
        """Write what the loop hands over, on a thread of its own.

        A write to a pipe blocks when the pipe is full, and pi is a process this
        one starts and does not control: it can be loading a model, wedged, or
        simply not reading. That write was made from the event loop, and the loop
        is what reads the microphone, drains the queues and answers the dashboard,
        so one child that stopped reading took the whole assistant with it and no
        deadline anywhere was watching. The queue is bounded, so the same child is
        reported instead of waited for.
        """
        while True:
            line = self._writes.get()               # this thread exists to wait here
            if line is None or generation != self._generation:
                return
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                # the pipe is gone, or the process with it; the loop finds out on
                # its next send, and the reader thread reports the end
                self._write_error = f"{type(exc).__name__}: {exc}"
                return

    def _read_stdout(self, generation: int, proc):
        """Read the process this thread was started for, not whatever is current.

        A restart replaces self._proc, so a reader that looked the process up again
        could attach to the next one's pipe. The process is an argument for that,
        and because `assert self._proc.stdout` is no guard at all under `python -O`:
        the assert is not there and the line under it raises on None.
        """
        stream = proc.stdout
        if stream is None:
            return
        for line in stream:
            line = line.rstrip("\r\n")
            if line and generation == self._generation:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, line)
        if generation == self._generation:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)   # process ended

    def _read_stderr(self, generation: int, proc):
        stream = proc.stderr
        if stream is None:
            return
        for line in stream:
            self._stderr.append(line.rstrip())
            del self._stderr[:-40]              # keep the tail only

    def _stderr_tail(self, limit: int = 2) -> str:
        """What pi last said on its error stream, for when it dies without a word.

        This was collected from the day it was written and never read anywhere, so a
        pi that ended mid-turn reported "pi process ended unexpectedly" and nothing
        else, while the reason (a key it could not find, a flag it did not
        understand, a config file that would not parse) was sitting in a list nobody
        looked at. Anything secret in there is not: pi writes no request bodies to
        stderr, and this goes into a log line and a spoken sentence either way.
        """
        lines = [line.strip() for line in self._stderr if line.strip()]
        return " / ".join(lines[-limit:])[:200]

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -------------------------------------------------------------------- rpc
    @staticmethod
    def _prompt_payload(message: str, images: list[dict] | None = None) -> dict:
        prompt = {"type": "prompt", "message": message}
        if images:
            prompt["images"] = images
        return prompt

    def _send(self, obj: dict):
        """Hand one rpc message to the writer thread. Never blocks the loop."""
        if not self.alive():
            raise BrainError("pi process is not running")
        if self._write_error:
            raise BrainError(f"pi stopped reading what I sent it: {self._write_error}")
        writes = self._writes
        if writes is None:
            raise BrainError("pi process is not running")
        try:
            writes.put_nowait(json.dumps(obj) + "\n")
        except queue.Full:
            raise BrainError(
                f"pi has not read the last {WRITE_QUEUE} messages, so it is not "
                f"reading its input") from None

    def _answer_dialog(self, event: dict):
        """Never let an extension dialog block the assistant forever."""
        if event.get("method") in DIALOG_METHODS:
            self._send({"type": "extension_ui_response", "id": event.get("id"), "cancelled": True})

    async def ask(self, message: str, images: list[dict] | None = None):
        """Yield ('delta', text) as tokens arrive, ('tool', name) on tool use,
        then ('done', full_text). Images are pi rpc ImageContent dicts and need
        a model that can see; a blind model just answers from the text.

        A turn is bracketed by its own run. pi emits both `turn_end` (one model
        response, plus its tool results) and `agent_end` (the whole run), and a
        run can hold several turns when tools are involved, so `agent_end` is
        the only honest terminator. Returning at `turn_end` left one event
        behind every time, and the next question then read that leftover and
        came back empty in zero seconds, which is what a second question asked
        in quick succession used to do.

        Only one turn reads this process at a time. An interrupting turn sets
        the abort event, so the turn in flight leaves within a loop iteration
        and the new one starts with a queue that only holds its own events.
        """
        if not self.alive():
            raise BrainError("pi process is not running")
        if self._turn_lock is None:
            self._turn_lock = asyncio.Lock()
        async with self._turn_lock:
            async for item in self._one_turn(message, images):
                yield item

    async def _one_turn(self, message: str, images: list[dict] | None = None):
        self._aborted.clear()
        while not self._queue.empty():          # drop anything stale
            self._queue.get_nowait()

        self._request += 1
        request = f"turn-{self._request}"
        payload = self._prompt_payload(message, images)
        payload["id"] = request
        self._send(payload)

        started = False
        text = ""
        tool_running = False      # a tool is executing right now
        tool_used = False         # this turn has used one at all
        tool_calls = 0            # lookups this turn, against the cap below
        turn_started = time.monotonic()
        steered = False
        while True:
            if self._aborted.is_set():
                return                          # interrupted: the caller is done
            # A deadline on silence, not on the whole turn: a long tool run that
            # keeps talking is fine, a process that has stopped answering is not.
            # While a tool is in flight the process is demonstrably working, so
            # it gets the longer deadline: a curl that takes twenty seconds is a
            # slow lookup, not a dead model, and killing it at the silence
            # deadline reported a healthy model as a dropped connection and threw
            # the lookup away. The longer deadline stays for the rest of the turn
            # once a tool has run, because the answer after a tool is a second
            # pass over results that are now in the context, and the first token
            # of that pass is measured in tens of seconds rather than two.
            limit = (config.BRAIN_TOOL_TIMEOUT_S if (tool_running or tool_used)
                     else config.BRAIN_SILENCE_TIMEOUT_S)
            try:
                line = await asyncio.wait_for(self._queue.get(), timeout=limit)
            except asyncio.TimeoutError:
                self.abort()
                raise BrainError(
                    f"pi said nothing for {limit:.0f}s"
                    f"{' while a tool was running' if tool_running else ''}"
                ) from None
            if line is ABORT:
                # the interrupt belongs to this turn, and it has just arrived
                return
            if line is None:
                tail = self._stderr_tail()
                raise BrainError("pi process ended unexpectedly"
                                 + (f": {tail}" if tail else ""))
            # Time as well as count: one slow fetch can cost more than several
            # quick ones. Checked per event, which is exactly when a tool has
            # just finished and the model is about to decide what to do next.
            if (not steered and tool_used
                    and time.monotonic() - turn_started > config.BRAIN_TURN_LOOKUP_S):
                steered = True
                self._send({
                    "type": "steer",
                    "message": ("That has taken long enough. Answer now, in one or "
                                "two sentences, with what you already have. Do not "
                                "run any more commands."),
                })
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            kind = event.get("type")
            # the acceptance of this prompt, which is the other place a turn can
            # fail before it starts: a model the relay refuses, a bad command
            if kind == "response" and event.get("id") == request:
                if not event.get("success", True):
                    raise BrainError(str(event.get("error") or "pi refused the prompt"))
                continue
            if not started:
                # anything before our run begins belongs to the run before this
                # one. Its errors must not be reported as this turn's failure,
                # and its trailing text must not be mistaken for this turn's
                # answer, which is why the start marker is a marker and not
                # "something happened": a delta left over from an interrupted
                # run read as this run starting, and the new turn then ended on
                # the old run's end marker with the old run's last word.
                if kind in ("agent_start", "turn_start"):
                    started = True
                    tool_running = False
                    continue
                if kind in ("agent_end", "turn_end", "error"):
                    continue
                if kind == "extension_ui_request":
                    self._answer_dialog(event)
                continue

            if kind == "extension_ui_request":
                self._answer_dialog(event)
            elif kind == "message_update":
                delta = event.get("assistantMessageEvent", {})
                if delta.get("type") == "text_delta":
                    chunk = delta.get("delta", "")
                    text += chunk
                    yield ("delta", chunk)
            elif kind == "tool_execution_start":
                tool_running = True
                tool_used = True
                tool_calls += 1
                if tool_calls == config.BRAIN_MAX_TOOLS and not steered:
                    steered = True
                    # asking nicely in the prompt did not hold, so say it here:
                    # pi delivers a steer message after the running tool finishes,
                    # before the next model call
                    self._send({
                        "type": "steer",
                        "message": ("That is enough looking up. Answer now, in one "
                                    "or two sentences, with what you already have. "
                                    "Do not run any more commands."),
                    })
                args = event.get("args") or {}
                detail = args.get("command") or args.get("path") or next(
                    (str(v) for v in args.values() if isinstance(v, str)), "")
                label = event.get("toolName", "tool")
                yield ("tool", f"{label}: {detail}"[:160] if detail else label)
            elif kind == "turn_end":
                # not the end of the run, only of this response. An error here is
                # still worth stopping for: the run will not produce more text.
                message_ev = event.get("message") or {}
                if message_ev.get("stopReason") == "error":
                    raise BrainError(
                        str(message_ev.get("errorMessage") or "the model failed"))
            elif kind == "tool_execution_end":
                tool_running = False
            elif kind == "agent_end":
                yield ("done", text.strip())
                return
            elif kind == "auto_retry_start":
                yield ("tool", "retrying")
            elif kind == "error":
                raise BrainError(str(event))

    def advance_model(self) -> str | None:
        """Step to the next model in the chain, wrap around, and name it."""
        self.model_index = (self.model_index + 1) % len(self.chain)
        # Which model is answering is part of what the gate enforces: moving from
        # the model on this disk to a relay is the moment file contents stop being
        # local, and the gate judges file tools by reading a file. Without this the
        # policy on disk still described the model that answered at start-up, so a
        # chain that moved to a relay kept reading files under a policy written for
        # the local one. note_for_gate existed for this and nothing called it.
        self.note_for_gate()
        return self.chain[self.model_index]

    def note_for_gate(self) -> None:
        """Tell the permission gate which model is answering, so it can judge files.

        The gate runs inside pi and cannot see the chain from there, and a chain
        that moves from a relay to the local model changes whether file contents are
        leaving the machine at all. Called on every move, including the two that
        only happen mid-conversation: a fallback down the chain, and the switch to
        the local model when the network goes. A failure here is reported and not
        fatal, because the alternative is a turn that dies on a file write.
        """
        if not permissions.enabled():
            return
        permissions.note_model(self.model_name())
        try:
            permissions.write_policy()
        except (permissions.GateMissing, OSError) as exc:
            print(f"[gate] could not write the policy for {self.model_name()}: {exc}",
                  flush=True)

    def model_name(self) -> str:
        """The model answering now. Empty when there is no chain at all.

        `start` refuses to run with nothing to ask and says which setting to fill
        in, and the assistant carries on without a brain on purpose, so there is a
        window where the chain really is empty. `self.chain[0 % 0]` raised
        ZeroDivisionError in that window, from the privacy check in `_process`,
        before every request about the screen: the honest "no model is configured"
        was never reached. Every caller reads an empty name as somebody else's
        machine, which is the safe direction for that question.
        """
        if not self.chain:
            return ""
        return self.chain[self.model_index % len(self.chain)]

    def on_local_model(self) -> bool:
        return self._is_local(self.model_name())

    def switch_chain(self) -> str:
        """Recompute the chain for the network as it looks now, and start at its head.

        Used when a turn proves the internet is gone. Walking the online chain
        one silence deadline at a time would spend a minute of the user's time to
        learn what one failure already said, and the local model was never tried.
        """
        self.chain = model_chain()
        self.model_index = 0
        self.note_for_gate()            # the gate judges files by which model is answering
        return self.model_name()

    async def new_session(self):
        self._send({"type": "new_session"})

    def abort(self):
        """Stop whatever the agent is doing, used when the user interrupts.

        The event ends the turn reading this process, so the next one does not
        have to share a queue with it. The command tells pi to stop generating.
        """
        self._aborted.set()
        self._wake()
        try:
            self._send({"type": "abort"})
        except BrainError:
            pass                                # nothing running to interrupt

    def _wake(self):
        """Wake the turn parked on the queue, so an interrupt is not waited out.

        The turn in flight is inside `await self._queue.get()` and looks at the
        abort flag only after an event arrives, so setting the flag alone left it
        there until pi answered or the silence deadline fired, holding the one-turn
        lock the whole time. The request the user spoke after interrupting was
        queued behind it in silence, which is the failure an interrupt exists to
        prevent: measured here, the deadline is twenty five seconds of a turn
        nobody is waiting for. Silence is not an event, so the abort is put in the
        queue as one.
        """
        waiting = getattr(self, "_queue", None)
        if waiting is None:
            return
        loop = getattr(self, "_loop", None)
        try:
            here = asyncio.get_running_loop()
        except RuntimeError:
            here = None
        if here is not None and here is loop:
            # put it in now rather than on the next pass of the loop: the turn
            # that just hit its deadline starts its replacement as soon as it
            # returns, and its first act is to drop what is already queued. A
            # marker that arrives after that drain would end the new turn
            # instead of the old one.
            waiting.put_nowait(ABORT)
        elif loop is not None:
            loop.call_soon_threadsafe(waiting.put_nowait, ABORT)

    def stop(self):
        if self._proc:
            try:
                if self._writes is not None:
                    # let the writer thread leave its loop rather than closing the
                    # pipe under it
                    try:
                        self._writes.put_nowait(None)
                    except queue.Full:
                        pass
                self._proc.stdin and self._proc.stdin.close()
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
                # and wait for it. A killed child that is never waited for stays a
                # zombie until this process exits, and this restarts the brain
                # rather than exiting: one zombie per hard restart, each holding a
                # process slot, on a machine that runs for days.
                try:
                    self._proc.wait(timeout=5)
                except Exception:
                    pass
            self._proc = None
