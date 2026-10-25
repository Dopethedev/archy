"""The assistant loop.

Four states, one audio pump:

  IDLE      listening only for the wake phrase
  LISTENING recording the request, ending it when you stop talking
  THINKING  transcribing, then waiting on pi
  SPEAKING  playing the reply

The pump never blocks. Transcription, the model and the voice all run off the
loop, so the microphone queue keeps draining in every state and stale audio
never leaks into the next turn. The brain stays on the loop itself because its
queues belong to that loop.
"""

from __future__ import annotations

import asyncio
import queue
import re
import threading
import time

import numpy as np

from . import config
from . import briefing
from . import reminders as reminders_mod
from . import mic_state
from . import sweep as sweep_mod
from .audio import MicCapture, Player
from .brain import BrainError, PiBrain
from .hud import Hud
from .stt import Transcriber
from .tts import Speaker
from .vad import SpeechVAD
from .vision import ScreenError, as_image, capture, wanted
from .wake import SpeechWake, WakeWord


def blip(freq=880.0, ms=70, rate=config.HUD_RATE) -> np.ndarray:
    """A short quiet tone, so the wake word gives instant feedback."""
    t = np.linspace(0, ms / 1000, int(rate * ms / 1000), dtype=np.float32)
    tone = np.sin(2 * np.pi * freq * t).astype(np.float32)
    ramp = np.minimum(1.0, np.minimum(np.arange(tone.size),
                                      tone.size - np.arange(tone.size)) / 240).astype(np.float32)
    return tone * ramp * 0.25


class Assistant:
    def __init__(self, hud: Hud | None = None, greet: str = ""):
        self.hud = hud
        self.greet = greet
        self.state = "booting"
        self.mic = MicCapture()
        self.player = Player()
        self.transcriber = Transcriber()
        self.speaker = Speaker(self.player)
        # In speech mode the phrase is understood rather than matched, so the
        # pretrained detector is not even loaded.
        self.wake = WakeWord() if config.WAKE_MODE == "model" else None
        self.speech_wake = SpeechWake() if config.WAKE_MODE == "speech" else None
        self.brain = PiBrain()
        self._vad = None
        self._typed: queue.Queue[str] = queue.Queue()
        self._stopping = False
        self._cancel = threading.Event()      # set when the user interrupts
        self._speaking_since = 0.0
        self._levels_at = 0.0
        self.last_heard = ""
        self.turn_latency = 0.0               # spoken delay of the last turn
        self.mic_enabled = True               # what the dashboard's toggle controls
        self._deaf_warned = False             # said once, not every twenty seconds
        self._hang_count = 0                  # silent turns in a row on this model
        self._loop: asyncio.AbstractEventLoop | None = None
        # the event loop keeps only weak references to tasks; a turn whose task
        # nobody holds could in principle be collected mid-flight, so every
        # spawned task is pinned here until it finishes
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> asyncio.Task:
        """create_task with a held reference, so a turn cannot vanish."""
        # tests build this object with __new__ and no __init__, so the set may
        # not exist yet; create it lazily rather than sharing one at class level
        if getattr(self, "_tasks", None) is None:
            self._tasks = set()
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # --------------------------------------------------------- local intents
    # A theme command is the name of a theme, optionally followed by the word
    # mode or theme, with nothing around it that turns the sentence into a
    # question about themes or an opinion of one. Refusing is the hard half: a
    # sentence about a game contains exactly the same words as a command, and a
    # matcher that only ever fires repaints the screen in the middle of someone
    # talking about Elden Ring. What separates them is what surrounds the name,
    # so both halves are tested with the same phrases.
    _THEME_NOUNS = frozenset(("mode", "theme", "profile", "palette"))
    # right after the name, these mean the sentence is describing a theme
    _THEME_DESCRIBES = frozenset((
        "is", "was", "are", "were", "isn't", "aren't", "has", "have", "be",
        "been", "does", "do", "did", "in", "for", "of", "with", "on", "at",
        "when", "that", "which", "because", "than", "if", "so", "then",
        "while", "and", "or", "but", "vs", "versus", "good", "better", "best",
        "great", "bad", "worse", "worst", "useful", "worth", "fun", "cool",
        "nice", "awesome", "work", "works", "working", "help", "helps",
        "matter", "matters", "one", "ones", "my", "your", "the", "a", "an",
        "game", "games", "any",
    ))
    # right before the name, these make it a question or an opinion
    _THEME_ASKS = frozenset((
        "is", "are", "was", "were", "does", "do", "did", "what", "what's",
        "how", "why", "which", "when", "who", "whether", "about", "like",
        "love", "hate", "prefer", "enjoy", "think", "that's", "it's", "that",
        "this", "everything",
    ))
    # a bare "stealth" is only a command when something is asking for it
    _THEME_VERBS = frozenset((
        "go", "switch", "change", "set", "put", "make", "turn", "use", "enable",
        "activate", "enter", "engage", "give", "take", "restore", "reset",
        "return", "back", "want", "let", "please", "prefer", "keep", "to",
    ))

    @classmethod
    def _theme_intent(cls, text: str) -> str | None:
        """A dashboard repaint by voice, or nothing at all."""
        words = re.findall(r"[a-z0-9']+", text.lower())
        if not words:
            return None
        joined = " ".join(words)
        if any(phrase in joined for phrase in config.THEME_RESET_PHRASES):
            return "standard"

        # the last name in the sentence: a command ends on the name it wants
        found = None
        for theme, names in config.THEMES.items():
            for name in names:
                parts = name.split()
                for i in range(len(words) - len(parts) + 1):
                    if words[i:i + len(parts)] == parts and (found is None or i > found[0]):
                        found = (i, len(parts), theme)
        if found is None:
            return None

        i, n, theme = found
        head, rest = words[:i], words[i + n:]
        named = bool(rest) and rest[0] in cls._THEME_NOUNS
        tail = rest[1:] if named else rest
        if tail and tail[0] in cls._THEME_DESCRIBES:
            return None                    # describing it, not asking for it
        if head and head[-1] in cls._THEME_ASKS:
            return None                    # "is stealth mode ...", "about combat mode"
        # a name with mode after it, or two words long, or with a verb before it,
        # is a command; a bare one-word name on its own is not
        if named or n > 1 or (head and head[-1] in cls._THEME_VERBS):
            return theme
        return None

    # ------------------------------------------------------------------- hud
    def set_state(self, state: str):
        self.state = state
        # The listening window is the only place where a quiet word is lost for
        # good, so it is the only place that listens harder. Leaving it on while
        # it speaks would feed the barge detector a hotter signal, and it could
        # start interrupting itself.
        if getattr(self, "mic", None) is not None:
            self.mic.set_focus(state == "listening")
        if self.hud:
            self.hud.publish({"type": "state", "value": state})

    def log(self, who: str, text: str):
        print(f"[{time.strftime('%H:%M:%S')}] {who}: {text}", flush=True)
        if self.hud:
            self.hud.publish({"type": "log", "who": who, "text": text})

    LEVEL_MIN_GAP = 0.05                  # 20 a second; the orb smooths anyway

    def _publish_levels(self, force: bool = False):
        """Send the microphone and output level to the dashboard.

        The pump runs every 10 ms and used to publish every time, which was a
        hundred events a second of JSON over SSE: five times more traffic than
        the eye can use, and enough to wake the browser constantly.
        """
        if not self.hud:
            return
        now = time.monotonic()
        if not force and now - self._levels_at < self.LEVEL_MIN_GAP:
            return
        self._levels_at = now
        self.hud.publish({"type": "level", "mic": round(min(self.mic.level, 1.0), 3),
                          "out": round(min(self.player.level, 1.0), 3)})

    def _publish_heard(self, text: str, partial: bool = False):
        """Show what was understood, while it is still fresh."""
        if self.hud and text:
            self.hud.publish({"type": "heard", "text": text, "partial": partial})

    # ------------------------------------------------------------------ vad
    def speech_score(self, frame: np.ndarray) -> float:
        if self._vad is None:
            self._vad = SpeechVAD()
        return self._vad.speech(frame.astype(np.int16), frame_size=config.INPUT_BLOCK)

    # ------------------------------------------------------------- utterances
    async def _check_wake(self, audio: np.ndarray, transcript: str | None):
        """Speech mode: was that the wake phrase, and did a request arrive with it?

        This is what replaces a trained detector. It also means "hey jarvis, open
        sober" works in one breath, since anything after the phrase is the
        request.
        """
        try:
            if transcript is None:
                transcript = await asyncio.to_thread(self.transcriber.transcribe, audio)
            heard, rest = self.speech_wake.match(transcript)
            if not heard:
                if transcript:
                    print(f"[wake] not for me: {transcript[:70]!r}")
                self._back_to_idle()
                return

            self._publish_heard(transcript)
            self.player.write(blip())
            self.log("sys", f"wake phrase: {transcript[:50]!r}")
            rest = self.speech_wake.strip_greeting(rest)
            if len(rest) >= 3:
                await self._process(text=rest)      # the request came with it
            else:
                self.mic.drain()
                self.set_state("listening")         # armed, waiting for the request
        except Exception as exc:
            print(f"[wake] {type(exc).__name__}: {exc}")
            self._back_to_idle()

    def _begin_listening(self):
        """Enter listening state. Shared by the wake word and by an interruption."""
        self.mic.drain()
        self.set_state("listening")          # which is also what enables focus

    def _interrupt(self):
        """Cut off a reply in progress, because the user has spoken."""
        self._cancel.set()
        self.player.flush()
        self.brain.abort()
        self.log("sys", "interrupted")

    def _start_utterance(self, audio: np.ndarray, speech_ms: float, transcript: str | None = None):
        """Called when the user stops talking, or the recording hits its limit."""
        if speech_ms < config.MIN_SPEECH_MS:
            self.log("sys", "too short, ignoring")
            self._back_to_idle()
            return
        self.set_state("thinking")                      # claim the state before yielding
        self._spawn(self._process(audio=audio, text=transcript))

    async def _speculate(self, audio: np.ndarray) -> str | None:
        """Transcribe while the user is still pausing, off the critical path."""
        try:
            text = await asyncio.to_thread(self.transcriber.transcribe, audio)
        except Exception:
            return None
        # the dashboard shows it straight away: the words are already final, so
        # waiting for the turn to finish only makes the screen look dead
        if text:
            self._publish_heard(text, partial=True)
        return text

    async def _process(self, audio: np.ndarray | None = None, text: str | None = None):
        deltas: queue.Queue[str | None] = queue.Queue()
        spoken = threading.Event()
        # a fresh token per turn: an interrupted turn must not be able to clear
        # the state that its replacement has already moved into
        cancel = threading.Event()
        self._cancel = cancel
        loop = asyncio.get_running_loop()

        def on_first_audio():
            if not spoken.is_set():
                spoken.set()
                self._hang_count = 0          # this model is answering after all
                self._speaking_since = loop.time()
                self.turn_latency = loop.time() - turn_started
                self.log("sys", f"answered in {self.turn_latency:.1f}s")
                if self.hud:
                    self.hud.publish({"type": "turn", "latency": round(self.turn_latency, 2)})
                loop.call_soon_threadsafe(self.set_state, "speaking")

        def delta_iter():
            while True:
                item = deltas.get()
                if item is None:
                    return
                yield item

        turn_started = loop.time()
        try:
            if text is None:
                text = await asyncio.to_thread(self.transcriber.transcribe, audio)
            if cancel.is_set():
                return
            if not text.strip():
                self.log("sys", "no words in that, ignoring")
                return
            self.last_heard = text
            self.log("you", text)
            self._publish_heard(text)

            # Local intents are answered without the model: a reminder, a
            # timer, a theme. One breath, no relay in the way.
            local_reply = None
            action = reminders_mod.parse(text)
            if action:
                local_reply = reminders_mod.confirm(action)
            elif (back := reminders_mod.undo_intent(text)):
                local_reply = back
            elif (hint := reminders_mod.cannot_do(text)):
                # a reminder this cannot serve is one a model can only pretend
                # to accept, so it is answered here instead of passed on
                local_reply = hint
            else:
                theme = self._theme_intent(text)
                if theme:
                    if self.hud:
                        self.hud.publish({"type": "theme", "value": theme})
                    local_reply = {
                        "standard": "Back to standard.",
                        "stealth": "Stealth mode engaged.",
                        "combat": "Combat mode engaged.",
                    }[theme]

            # A request about the screen gets the screen attached; a request
            # for a security sweep gets fresh results to talk about; a request
            # for a briefing gets today's facts. The sweep wins if several
            # match: nobody screenshots while auditing.
            images = None
            sweep_report = None
            brief_facts = None
            if sweep_mod.wanted(text) and not sweep_mod.supported():
                # Nothing on this machine for the checks to read, so it is
                # answered here instead of after a long look at nothing.
                local_reply = ("The sweep reads a Linux machine or a Mac: the "
                               "processes, the things that start at login, and "
                               "what is listening. There is nothing on this one "
                               "for it to read.")
            elif sweep_mod.wanted(text):
                deltas.put("Running the sweep now, this takes a few seconds.")
                try:
                    sweep_report = await asyncio.wait_for(
                        asyncio.to_thread(sweep_mod.run), timeout=180.0)
                except asyncio.TimeoutError:
                    sweep_report = None
                    self.log("sys", "the sweep took too long, summarising without it")
            elif wanted(text):
                try:
                    jpeg = await asyncio.to_thread(capture)
                    images = [as_image(jpeg)]
                    self.log("sys", "looking at your screen")
                except (ScreenError, OSError) as exc:
                    self.log("sys", f"could not capture the screen: {exc}")
            elif briefing.wanted(text):
                brief_facts = await asyncio.to_thread(
                    briefing.gather, not briefing.wants_light(text))

            tts = threading.Thread(target=self.speaker.say_stream,
                                   args=(delta_iter(), on_first_audio, cancel), daemon=True)
            tts.start()

            full = ""
            if local_reply is not None:
                deltas.put(local_reply)
                deltas.put(None)
                full = local_reply
            else:
                prompt = text
                if sweep_report:
                    prompt = (f"{text}\n\nFRESH SECURITY SWEEP RESULTS, run just "
                              f"now on this machine:\n{sweep_report}\n\nSummarise "
                              "for speech. These results are current and any "
                              "earlier sweep in this conversation is out of date. "
                              "Lead with anything that needs action, then the "
                              "clean parts in one breath. Be honest about what "
                              "the sweep does not cover.")
                elif brief_facts:
                    # The facts are read a moment before this line, but the model
                    # also has the whole conversation, which on a long session
                    # holds yesterday's numbers. Asked "how is the machine", it
                    # answered "up over twelve hours" from its memory of an
                    # earlier briefing while the facts it was given said two
                    # hours: the number it repeated was confidently wrong. So it
                    # is told which of the two to trust.
                    prompt = (f"{text}\n\n{brief_facts}Answer from these facts: "
                              "they were read just now, and anything from earlier "
                              "in this conversation is out of date. One or two "
                              "sentences, plain prose.")
                try:
                    async for kind, payload in self.brain.ask(prompt, images):
                        if cancel.is_set():
                            break
                        if kind == "delta":
                            deltas.put(payload)
                        elif kind == "tool":
                            self.log("doing", payload)
                        elif kind == "done":
                            full = payload
                finally:
                    deltas.put(None)

            if full and not cancel.is_set():
                self.log("jarvis", full)
            elif not full and not cancel.is_set():
                self.log("sys", "the model returned nothing")
            await asyncio.to_thread(tts.join, 180)
            if cancel.is_set():
                return                       # the interruption owns the state now
            await asyncio.to_thread(self.player.wait_drained, 180)

        except BrainError as exc:
            print(f"[brain] {exc}")
            # A dead relay deserves the next one in the chain. A silent model is
            # not proof on its own that the model died, because a slow tool run
            # looks the same from here, so it gets one retry before the chain
            # moves on. Without the second half of that rule a relay that
            # accepts a connection and then says nothing is retried forever, and
            # every question is answered with "my connection dropped".
            silent = "said nothing for" in str(exc)
            self._hang_count = self._hang_count + 1 if silent else 0
            if not silent or self._hang_count >= config.MODEL_HANG_LIMIT:
                moved = self.brain.advance_model()
                self._hang_count = 0
            else:
                moved = None
            if moved:
                reason = ("went quiet twice" if silent else "failed")
                self.log("sys", f"the model {reason}, switching to "
                                f"{moved.split('/')[-1]}")
                message = ("My connection dropped, so I switched to a backup "
                           "model. Ask me again.")
            else:
                self.log("sys", "the model connection dropped, restarting it")
                message = "My connection to the model dropped. Ask me again."
            # both of these block for seconds while they synthesise, and
            # blocking the loop here backs up the microphone queue: the pump then
            # replays stale audio as if it were fresh, and the tail of his own
            # voice can come back as a phantom second request
            self.log("jarvis", message)      # spoken fallbacks belong in the record
            await asyncio.to_thread(self.speaker.say, message)
            await asyncio.to_thread(self.player.wait_drained, 60)
            await self._restart_brain()
        except Exception as exc:
            print(f"[assistant] {type(exc).__name__}: {exc}")
            self.log("jarvis", "Something went wrong there.")
            await asyncio.to_thread(self.speaker.say, "Something went wrong there.")
            await asyncio.to_thread(self.player.wait_drained, 60)
        finally:
            if not cancel.is_set():
                self._back_to_idle()

    # ------------------------------------------------------------------ mic
    def _request_mic(self, on: bool):
        """Called from the dashboard's HTTP thread, so hand it to the loop."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self.set_mic_enabled, bool(on))

    def set_mic_enabled(self, on: bool):
        """Open or close the microphone. Nothing reaches the assistant when off.

        Turning it off also drops the state to muted, which the dashboard shows
        as its own colour, so a dead microphone never looks like a live one.
        """
        on = bool(on)
        if on == self.mic_enabled:
            return
        self.mic_enabled = on
        mic_state.save(on)
        if on:
            try:
                self.mic.start()
            except Exception as exc:
                self.log("sys", f"could not open the microphone: {exc}")
                self.mic_enabled = False
                return
            self.log("sys", "microphone on")
            self._back_to_idle()
        else:
            self.mic.stop()
            self.log("sys", "microphone off")
            self.set_state("muted")

    # ------------------------------------------------------------- reminders
    async def _announce(self, text: str):
        """Say something with no request behind it, out loud.

        Used for the microphone that is delivering nothing: a notice in the
        dashboard is no use to someone who is waiting to be answered.
        """
        if self._stopping or self.state != "idle":
            return
        self.set_state("speaking")
        try:
            await asyncio.to_thread(self.speaker.say, text)
            self.log("jarvis", text)
            await asyncio.to_thread(self.player.wait_drained, 30)
        except Exception as exc:
            print(f"[announce] {exc}")
        finally:
            if self._stopping:
                return
            self._back_to_idle()

    async def _reminder_loop(self):
        """Due reminders are spoken, out loud, whatever the dashboard shows.
        The loop waits for a quiet moment rather than talking over a reply."""
        while not self._stopping:
            await asyncio.sleep(config.REMINDER_CHECK_S)
            try:
                ripe = reminders_mod.due_items()
            except Exception as exc:
                print(f"[reminders] {exc}")
                continue
            for label in ripe:
                while (self.state in ("thinking", "speaking", "listening")
                       and not self._stopping):
                    await asyncio.sleep(1.0)
                if self._stopping:
                    return
                self.set_state("speaking")
                self.player.write(blip(660.0))
                self.player.write(blip(990.0))
                text = reminders_mod.spoken(label)
                self.log("jarvis", text)
                try:
                    ok = await asyncio.to_thread(self.speaker.say, text)
                    await asyncio.to_thread(self.player.wait_drained, 60)
                except Exception as exc:
                    ok = False
                    print(f"[reminders] could not speak: {exc}")
                if not ok:
                    # the label was already removed from the store; a reminder
                    # that could not be spoken must come back, not vanish
                    print("[reminders] will retry in 60s")
                    reminders_mod.add(label, 60.0)
                self._back_to_idle()

    async def _restart_brain(self):
        try:
            await asyncio.to_thread(self.brain.stop)
            await self.brain.start()
        except Exception as exc:
            print(f"[brain] restart failed: {exc}")

    def _back_to_idle(self):
        self.mic.drain()
        if self.wake is not None:
            self.wake.reset()
        # muting outlives a single turn, so idle is only reachable with a live
        # microphone; otherwise the dashboard would say it is listening while
        # the microphone is closed
        self.set_state("idle" if self.mic_enabled else "muted")

    # ------------------------------------------------------------------ pump
    async def _pump(self):
        preroll: list[np.ndarray] = []
        utterance: list[np.ndarray] = []
        silence_ms = speech_ms = 0.0
        started = False
        listening_since = 0.0
        frame_ms = 1000.0 * config.INPUT_BLOCK / config.SAMPLE_RATE
        preroll_max = max(1, int(config.PRE_ROLL_MS / frame_ms))
        # kept while it is speaking, so an interruption does not swallow the
        # first word of the sentence that caused it
        barge_history: list[np.ndarray] = []
        barge_max = max(1, int(config.BARGE_HISTORY_MS / frame_ms))
        barge_run = 0
        # speculative transcription, started during the pause after speech stops
        spec_task: asyncio.Task | None = None
        spec_frames = 0
        spec_valid = False
        loop = asyncio.get_running_loop()

        def reset_buffers():
            nonlocal silence_ms, speech_ms, started, spec_task, spec_valid, spec_frames
            if spec_task is not None and not spec_task.done():
                spec_task.cancel()
            preroll.clear()
            utterance.clear()
            silence_ms = speech_ms = 0.0
            started = False
            spec_task = None
            spec_valid = False
            spec_frames = 0

        def recording_now(state: str) -> bool:
            """In speech mode, idle is also a recording state: that is where the
            wake phrase is understood from what was said."""
            if state == "listening":
                return True
            return state == "idle" and config.WAKE_MODE == "speech"

        prev_state = self.state

        while not self._stopping:
            # Notice state changes made by tasks before anything else, with or
            # without a frame arriving. The listening deadline is measured from
            # listening_since, so syncing it only when a frame happens to turn up
            # means a task that arms listening while the queue is momentarily
            # empty gets judged against a stale timestamp and gives up on the
            # spot: the reply was "nothing said, going quiet" in the same second
            # as the wake phrase.
            if self.state != prev_state:
                reset_buffers()
                if self.state == "listening":
                    listening_since = loop.time()
                prev_state = self.state
            state = self.state

            try:
                typed = self._typed.get_nowait()
            except queue.Empty:
                typed = None
            if typed:
                # typing while a reply is in progress must not run a second turn
                # alongside it: two turns means two voices and a state nobody owns
                if self.state in ("thinking", "speaking") and config.BARGE_IN:
                    self._interrupt()
                self.set_state("thinking")
                self._spawn(self._process(text=typed))

            # A microphone that has delivered nothing at all for twenty seconds
            # while it is sitting there listening is either switched off or
            # broken, and it will never hear a word. Saying so once is the
            # difference between an assistant that can be fixed and one that
            # looks like it is ignoring you.
            # tests build this object without __init__, so the flag may be absent
            # self.state, not the local copy: this runs before the loop reads the
            # current frame, so the local would be one iteration behind
            if (not getattr(self, "_deaf_warned", False)
                    and getattr(self, "mic_enabled", True) and self.state == "idle"
                    and self.mic.silent_seconds() > config.MIC_DEAF_S):
                self._deaf_warned = True
                self.log("sys", "the microphone is delivering silence: no "
                                "signal at all for twenty seconds")
                self._spawn(self._announce(
                    "I am not hearing anything from the microphone. "
                    "It may be switched off."))

            try:
                frame = self.mic.frames.get_nowait()
            except queue.Empty:
                # A deadline must not depend on audio arriving either. If the
                # stream stalls, waiting for a frame before noticing would wait
                # forever.
                if (state == "listening" and not started
                        and loop.time() - listening_since > config.LISTEN_TIMEOUT_S):
                    self.log("sys", "nothing said, going quiet")
                    reset_buffers()
                    self._back_to_idle()
                self._publish_levels()
                await asyncio.sleep(0.01)
                continue

            state = self.state

            # While it is talking, stillness is the default and the user is the
            # exception. Both the wake word and plain speech cut it off. Its own
            # voice does not reach the microphone as speech (measured), so this
            # cannot make it interrupt itself. The same applies while it is
            # thinking, where nothing of ours is playing at all, so a long tool
            # call can be redirected instead of waited out.
            bargeable = state == "speaking" or (state == "thinking" and config.BARGE_ON_THINKING)
            if bargeable:
                in_grace = (state == "speaking"
                            and loop.time() - self._speaking_since <= config.BARGE_GRACE_MS / 1000.0)
                if not config.BARGE_IN or in_grace:
                    barge_history.clear()
                    barge_run = 0
                    continue

                barge_history.append(frame)
                if len(barge_history) > barge_max:
                    barge_history.pop(0)

                woke = self.wake.push(frame, threshold=config.WAKE_DURING_SPEECH) if self.wake else False
                if config.BARGE_ON_SPEECH and self.speech_score(frame) >= config.SPEECH_ON:
                    barge_run += 1
                else:
                    barge_run = 0
                # Cutting off a reply should be easy. Cutting off the thinking
                # produces no sound at all, so a stray word would silently eat
                # the answer and there would be nothing to explain the silence.
                needed = (config.BARGE_FRAMES if state == "speaking"
                          else config.BARGE_FRAMES_THINKING)

                if woke or barge_run >= needed:
                    self._interrupt()
                    reset_buffers()
                    listening_since = loop.time()
                    self._begin_listening()
                    if barge_history:            # keep what triggered it
                        started = True
                        speech_ms = len(barge_history) * frame_ms
                        utterance.extend(barge_history)
                        barge_history = []
                    # Entering listening here is our own state change, and the
                    # loop resets its buffers when it sees one. Left to be seen,
                    # that second reset threw away the audio kept on the line
                    # above, so the first word of an interruption survived only
                    # when enough of it arrived afterwards to clear the minimum
                    # on its own: a short "stop" was discarded as too short and
                    # the answer was lost with it. Marking it seen keeps it.
                    prev_state = self.state
                    barge_run = 0
                continue

            # Model mode: idle is not a recording state, it is where the trained
            # detector listens. This has to be checked before the recording guard,
            # otherwise idle is skipped and the wake word can never fire.
            if state == "idle" and self.wake is not None:
                if self.wake.push(frame):
                    self.player.write(blip())
                    reset_buffers()
                    listening_since = loop.time()
                    self.log("sys", "wake word")
                    self._begin_listening()
                continue

            if not recording_now(state):
                continue                                    # drain, do not act

            score = self.speech_score(frame)
            # softer speech counts while it is waiting for a request
            on = config.SPEECH_ON_FOCUSED if state == "listening" else config.SPEECH_ON
            if not started:
                # The give-up timeout belongs to the listening state only. In
                # speech mode idle also records, and idle has no deadline: using
                # the timeout there spins, logging once per queued frame.
                if state == "listening" and loop.time() - listening_since > config.LISTEN_TIMEOUT_S:
                    self.log("sys", "nothing said, going quiet")
                    self._back_to_idle()
                    continue
                preroll.append(frame)
                if len(preroll) > preroll_max:
                    preroll.pop(0)
                if score >= on:
                    started = True
                    speech_ms = frame_ms
                    utterance.extend(preroll)
                    preroll.clear()
                continue

            utterance.append(frame)
            if score >= on:
                speech_ms += frame_ms
                silence_ms = 0.0
                spec_valid = False          # he spoke again, any snapshot is stale
            else:
                silence_ms += frame_ms

            # He has been quiet long enough that the rest of this utterance is
            # silence, so a transcript from here is already the final one. Only
            # worth doing while there is room left before the hard limit.
            total_ms = len(utterance) * frame_ms
            # a wake phrase should resolve faster than a full request
            end_ms = (config.END_SILENCE_MS if state == "listening"
                      else config.WAKE_IDLE_SILENCE_MS)
            # In the wake path a phrase is short, so the cap bounds how long a
            # recording can grow when the room never goes quiet.
            limit_ms = (config.MAX_UTTERANCE_S * 1000 if state == "listening"
                        else config.WAKE_MAX_UTTERANCE_S * 1000)
            # A recording that grows to the cap without a silence gap is not
            # thrown away: continuous video speech is exactly when a wake phrase
            # needs hearing, and the serialised transcriber bounds the cost at
            # one transcription per cap. The end-of-speech path below handles it
            # like any other utterance.
            if (spec_task is None
                    and silence_ms >= config.SPECULATE_AT_MS
                    and total_ms < limit_ms - 250):
                snapshot = np.concatenate(utterance).astype(np.float32) / 32768.0
                spec_frames = len(utterance)
                spec_valid = True
                spec_task = asyncio.create_task(self._speculate(snapshot))

            if silence_ms >= end_ms or total_ms >= limit_ms:
                audio = np.concatenate(utterance).astype(np.float32) / 32768.0
                transcript = None
                # Use the speculative result only if nothing but silence followed
                # it, and only if it actually produced words.
                if spec_task is not None and spec_valid and spec_task.done():
                    extra = (len(utterance) - spec_frames) * frame_ms
                    if extra <= end_ms + 2 * frame_ms:
                        try:
                            candidate = spec_task.result()
                        except Exception:
                            candidate = None
                        if candidate:
                            transcript = candidate
                            self.log("sys", "transcript was ready already")
                # Never wait for a speculation: if it has not finished, transcribing
                # normally is exactly as fast as it would have been without it.
                if spec_task is not None and not spec_task.done():
                    spec_task.cancel()
                spec_task = None

                if state == "listening":
                    if speech_ms < config.MIN_SPEECH_MS:
                        # A blip is not a request. Keep waiting for a real one
                        # rather than making you say the wake phrase again; the
                        # deadline still runs, so silence still ends it.
                        self.log("sys", "too short, ignoring")
                        reset_buffers()
                        continue
                    self._start_utterance(audio, speech_ms, transcript)
                elif speech_ms >= config.MIN_SPEECH_MS:
                    # idle in speech mode: this utterance may be the wake phrase.
                    # A cough or a click is not worth transcribing.
                    self.set_state("thinking")
                    self._spawn(self._check_wake(audio, transcript))
                else:
                    self._back_to_idle()

    async def _warm_up(self):
        """Run one real inference through the recogniser.

        The first call carries the model's lazy initialisation. Doing it here
        means the user's first question does not pay for it.
        """
        try:
            noise = (np.random.default_rng(0).standard_normal(config.SAMPLE_RATE) * 0.02)
            await asyncio.to_thread(self.transcriber.transcribe, noise.astype(np.float32))
            print("[warm] recogniser ready")
        except Exception as exc:
            print(f"[warm] skipped: {exc}")

    # -------------------------------------------------------------------- run
    async def run(self):
        self._loop = asyncio.get_running_loop()
        if self.hud:
            self.hud.on_say = self._typed.put
            self.hud.on_mic = self._request_mic
        self._spawn(self._reminder_loop())
        # before the brain starts, because pi resumes the newest session it
        # finds and a session past the budget would be loaded, then immediately
        # stopped, which is a second of start-up spent on a conversation that
        # was going to be set aside anyway
        await asyncio.to_thread(self.brain.archive_sessions)
        await self.brain.start()
        self.player.start()
        if self.greet:
            self.set_state("speaking")
            await asyncio.to_thread(self.speaker.say, self.greet)
            await asyncio.to_thread(self.player.wait_drained, 30)
            self.log("jarvis", self.greet)
        # warming up and opening the microphone is not speech: leaving the state
        # on "speaking" made the dashboard claim it was talking for the whole
        # of start-up
        self.set_state("booting")
        await self._warm_up()
        # the switch is remembered: opening the microphone for someone who turned
        # it off is a silent decision about the room he is in
        if not mic_state.load():
            self.mic_enabled = False
            self.log("sys", "the microphone is still off from last time")
            self.set_state("muted")
        else:
            self.mic.start()
            self.mic.drain()
            self.log("sys", f"listening for \"{config.WAKE_PHRASE}\"")
            self._back_to_idle()
        try:
            await self._pump()
        finally:
            self.shutdown()

    def shutdown(self):
        self._stopping = True
        self.mic.stop()
        self.player.stop()
        self.brain.stop()
        if self.hud:
            self.hud.stop()
        print("\noffline.")
