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
import threading
import time

import numpy as np

from . import config
from .audio import MicCapture, Player
from .brain import BrainError, PiBrain
from .hud import Hud
from .stt import Transcriber
from .tts import Speaker
from .vad import SpeechVAD
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
        self._loop: asyncio.AbstractEventLoop | None = None

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

        This is what replaces a trained detector. It also means "hey maki, open
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
        asyncio.create_task(self._process(audio=audio, text=transcript))

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

            tts = threading.Thread(target=self.speaker.say_stream,
                                   args=(delta_iter(), on_first_audio, cancel), daemon=True)
            tts.start()

            full = ""
            try:
                async for kind, payload in self.brain.ask(text):
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
            self.log("sys", "the model connection dropped, restarting it")
            self.speaker.say("My connection to the model dropped. Ask me again.")
            self.player.wait_drained(timeout=60)
            await self._restart_brain()
        except Exception as exc:
            print(f"[assistant] {type(exc).__name__}: {exc}")
            self.speaker.say("Something went wrong there.")
            self.player.wait_drained(timeout=60)
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
                asyncio.create_task(self._process(text=typed))

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
            # In the wake path there is no reason to record for long: a phrase is
            # short. Dropping it here means continuous room noise cannot turn into
            # a queue of long recordings all waiting to be transcribed.
            limit_ms = (config.MAX_UTTERANCE_S * 1000 if state == "listening"
                        else config.WAKE_MAX_UTTERANCE_S * 1000)
            if state != "listening" and total_ms >= limit_ms:
                self.log("sys", "that was not a wake phrase, dropping it")
                # buffers must be cleared here too: the state stays idle, so the
                # state-change reset below never fires and this would repeat on
                # every remaining frame
                reset_buffers()
                self._back_to_idle()
                continue
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
                        # rather than making him say the wake phrase again; the
                        # deadline still runs, so silence still ends it.
                        self.log("sys", "too short, ignoring")
                        reset_buffers()
                        continue
                    self._start_utterance(audio, speech_ms, transcript)
                elif speech_ms >= config.MIN_SPEECH_MS:
                    # idle in speech mode: this utterance may be the wake phrase.
                    # A cough or a click is not worth transcribing.
                    self.set_state("thinking")
                    asyncio.create_task(self._check_wake(audio, transcript))
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
        await self.brain.start()
        self.player.start()
        if self.greet:
            self.set_state("speaking")
            self.speaker.say(self.greet)
            self.player.wait_drained(timeout=30)
            self.log("jarvis", self.greet)
        # warming up and opening the microphone is not speech: leaving the state
        # on "speaking" made the dashboard claim it was talking for the whole
        # of start-up
        self.set_state("booting")
        await self._warm_up()
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
