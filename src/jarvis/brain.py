"""The brain: one long-lived `pi --mode rpc` process.

Spawned once and kept alive, so the conversation has memory and there is no
per-request process start-up. Responses stream in token by token, which lets
speech begin before the reply is finished.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading

from . import config
from .memory_store import Memory

DIALOG_METHODS = {"select", "confirm", "input", "editor"}


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
        # where we are in the model chain. The free relays die without warning
        # (a model that answered at breakfast 404s by lunch), so the assistant
        # moves down the chain and says so rather than failing
        self.model_index = 0

    @staticmethod
    def _system_prompt() -> str:
        """Voice rules, plus how to use the durable store, plus what is in it.

        The stored facts go into the prompt itself so that remembering costs no
        tool call and no round trip. It is capped, because a prompt that grows
        without bound would slow every turn down.
        """
        python = sys.executable
        how = (
            f" You keep durable facts about Bishesh in {config.MEMORY_FILE}. "
            f"Read them with `{python} -m jarvis.memory_store list`, add one with "
            f"`{python} -m jarvis.memory_store set NAME \"VALUE\"`, and remove one "
            "with `forget NAME`. Whenever he asks you to remember, note, or keep "
            "something, store it there, because the conversation alone will not "
            "survive. Do it quietly without announcing the mechanism.\n"
        )
        try:
            memory = Memory()
            if memory.line_count():
                facts = memory.summary()
                if len(facts) > 1200:
                    facts = facts[:1200] + " ..."
                how += f"\nWhat you already know about him: {facts}\n"
        except Exception as exc:
            print(f"[memory] could not load: {exc}")
        return config.VOICE_RULES + how

    # ------------------------------------------------------------------ setup
    async def start(self):
        config.SESSION_DIR.mkdir(exist_ok=True)
        self._generation += 1
        generation = self._generation
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()

        args = [
            config.PI_BIN, "--mode", "rpc",
            "--session-dir", str(config.SESSION_DIR),
            "--thinking", config.PI_THINKING,
            "--append-system-prompt", self._system_prompt(),
        ]
        model = config.PI_MODELS[self.model_index % len(config.PI_MODELS)]
        args += ["--model", model]
        print(f"[brain] model {model}", flush=True)
        if config.PI_CONTINUE:
            args.append("--continue")          # pick the conversation back up
        if not config.PI_EXTENSIONS:
            args.append("--no-extensions")

        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, args=(generation,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(generation,), daemon=True).start()

    def _read_stdout(self, generation: int):
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = line.rstrip("\r\n")
            if line and generation == self._generation:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, line)
        if generation == self._generation:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)   # process ended

    def _read_stderr(self, generation: int):
        assert self._proc and self._proc.stderr
        for line in self._proc.stderr:
            self._stderr.append(line.rstrip())
            del self._stderr[:-40]              # keep the tail only

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
        if not self.alive():
            raise BrainError("pi process is not running")
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(json.dumps(obj) + "\n")
        self._proc.stdin.flush()

    def _answer_dialog(self, event: dict):
        """Never let an extension dialog block the assistant forever."""
        if event.get("method") in DIALOG_METHODS:
            self._send({"type": "extension_ui_response", "id": event.get("id"), "cancelled": True})

    async def ask(self, message: str, images: list[dict] | None = None):
        """Yield ('delta', text) as tokens arrive, ('tool', name) on tool use,
        then ('done', full_text). Images are pi rpc ImageContent dicts and need
        a model that can see; a blind model just answers from the text."""
        if not self.alive():
            raise BrainError("pi process is not running")
        while not self._queue.empty():          # drop anything stale
            self._queue.get_nowait()

        self._send(self._prompt_payload(message, images))
        text = ""
        while True:
            # A deadline on silence, not on the whole turn: a long tool run that
            # keeps talking is fine, a process that has stopped answering is not.
            try:
                line = await asyncio.wait_for(self._queue.get(),
                                              timeout=config.BRAIN_SILENCE_TIMEOUT_S)
            except asyncio.TimeoutError:
                self.abort()
                raise BrainError(
                    f"pi said nothing for {config.BRAIN_SILENCE_TIMEOUT_S:.0f}s"
                ) from None
            if line is None:
                raise BrainError("pi process ended unexpectedly")
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            kind = event.get("type")
            if kind == "extension_ui_request":
                self._answer_dialog(event)
            elif kind == "message_update":
                delta = event.get("assistantMessageEvent", {})
                if delta.get("type") == "text_delta":
                    chunk = delta.get("delta", "")
                    text += chunk
                    yield ("delta", chunk)
            elif kind == "tool_execution_start":
                args = event.get("args") or {}
                detail = args.get("command") or args.get("path") or next(
                    (str(v) for v in args.values() if isinstance(v, str)), "")
                label = event.get("toolName", "tool")
                yield ("tool", f"{label}: {detail}"[:160] if detail else label)
            elif kind == "agent_end":
                yield ("done", text.strip())
                return
            elif kind == "turn_end":          # newer pi ends the turn with this
                message = event.get("message") or {}
                if message.get("stopReason") == "error":
                    raise BrainError(
                        str(message.get("errorMessage") or "the model failed"))
                yield ("done", text.strip())
                return
            elif kind == "auto_retry_start":
                yield ("tool", "retrying")
            elif kind == "error":
                raise BrainError(str(event))

    def advance_model(self) -> str | None:
        """Step to the next model in the chain, wrap around, and name it."""
        self.model_index = (self.model_index + 1) % len(config.PI_MODELS)
        return config.PI_MODELS[self.model_index]

    def model_name(self) -> str:
        return config.PI_MODELS[self.model_index % len(config.PI_MODELS)]

    async def new_session(self):
        self._send({"type": "new_session"})

    def abort(self):
        """Stop whatever the agent is doing, used when the user interrupts."""
        try:
            self._send({"type": "abort"})
        except Exception:
            pass

    def stop(self):
        if self._proc:
            try:
                self._proc.stdin and self._proc.stdin.close()
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None
