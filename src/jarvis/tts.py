"""Text to speech.

Kokoro is the voice: noticeably more natural than piper, and still about 3x
realtime on this CPU. piper stays as the fallback so a broken model file
degrades into a worse voice rather than silence.

Speech starts as soon as the first sentence exists rather than after the
whole reply arrives, which is most of what makes a voice feel responsive.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from . import config

MARKDOWN = [
    (re.compile(r"```.*?```", re.S), " "),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"[*_#>]+"), ""),
    (re.compile(r"^\s*[-•]\s*", re.M), ""),
    (re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]"), ""),
]
SENTENCE_END = re.compile(r"[.!?](?:\s|$)")
MIN_SENTENCE = 12
# The first chunk is cut far earlier than the rest. Kokoro synthesises at
# roughly real time, so waiting for a whole sentence before speaking costs
# exactly that sentence in silence. A clause is enough to start on.
FIRST_CHUNK_MIN = 12         # at least a couple of words, or it sounds clipped
FIRST_CHUNK_MAX = 48         # no punctuation in sight: take this much and go
CLAUSE_END = re.compile(r"[,;:]\s")
# A whole sentence is the natural unit to speak, but a long one is also a long
# wait: synthesis runs at about real time, so a 200 character sentence is three
# seconds of work. That matters most when a reply is interrupted, because the
# chunk being synthesised at that moment keeps burning CPU and cannot be
# cancelled. Capping the chunk bounds the waste.
SENTENCE_MAX = 90
SENTENCE_MIN = 40            # below this, prefer waiting for the sentence end


def clean(text: str) -> str:
    """Strip anything that reads badly out loud."""
    for pattern, repl in MARKDOWN:
        text = pattern.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip()


def split_ready(buffer: str, first: bool = False) -> tuple[str, str]:
    """Peel off the part of the buffer that is safe to speak now.

    With first=True the bar is a clause rather than a sentence, because nothing
    is audible yet and every millisecond of synthesis is a millisecond of the
    user waiting. Once the voice has started, whole sentences are better: they
    are smoother, and the delay no longer matters.
    """
    if first:
        cut = _first_cut(buffer)
        if cut > 0:
            return buffer[:cut], buffer[cut:]

    match = None
    for m in SENTENCE_END.finditer(buffer):
        if m.end() >= MIN_SENTENCE:
            match = m
    if match is not None:
        return buffer[:match.end()], buffer[match.end():]
    if len(buffer) > SENTENCE_MAX:            # a run-on with no end in sight
        cut = _tail_cut(buffer)
        if cut > 0:
            return buffer[:cut], buffer[cut:]
    return "", buffer



def _first_cut(buffer: str) -> int:
    """Where the opening clause ends, or 0 to wait for more text.

    The boundary lands after the punctuation and before the space, so the
    clause keeps its comma (which is what makes the prosody right) and the
    next chunk starts on a clean word.

    The clause has to be an early one. Taking the first comma wherever it falls
    meant a sentence that only punctuates near the end was held back whole, so a
    single late comma cost seconds before the first word was heard, which is the
    exact thing this function exists to avoid.
    """
    for match in CLAUSE_END.finditer(buffer):
        end = match.end() - 1
        if FIRST_CHUNK_MIN <= end <= FIRST_CHUNK_MAX:
            return end
    if len(buffer) >= FIRST_CHUNK_MAX:
        cut = buffer.rfind(" ", 0, FIRST_CHUNK_MAX)
        return cut if cut > 0 else 0
    return 0


def _tail_cut(buffer: str) -> int:
    """Where to break a sentence that is running long, or 0 to keep waiting.

    A clause boundary is the least audible place to stop, so take the last one
    inside the budget. Failing that, break at a space. The bound matters: a
    clause far past the cap is no better than no clause at all.
    """
    cut = 0
    for match in CLAUSE_END.finditer(buffer):
        end = match.end() - 1
        if SENTENCE_MIN <= end <= SENTENCE_MAX:
            cut = end
    if cut > 0:
        return cut
    cut = buffer.rfind(" ", 0, SENTENCE_MAX)
    return cut if cut >= SENTENCE_MIN else 0


class Speaker:
    def __init__(self, player, engine: str = config.TTS_ENGINE):
        self.player = player
        self.engine = engine
        self._kokoro = None
        self._piper = None
        if engine == "kokoro":
            from kokoro_onnx import Kokoro
            model = config.MODELS / "kokoro-v1.0.onnx"
            voices = config.MODELS / "voices-v1.0.bin"
            if model.exists() and voices.exists():
                self._kokoro = Kokoro(str(model), str(voices))
            else:
                self.engine = "piper"
        if self.engine == "piper":
            self._piper = Path(sys.executable).parent / "piper"
            if not self._piper.exists():
                raise RuntimeError("no usable speech engine: kokoro models missing and piper not found")

    # ---------------------------------------------------------------- engines
    def _synth_kokoro(self, text: str) -> np.ndarray:
        samples, sr = self._kokoro.create(
            text, voice=config.TTS_VOICE, speed=config.TTS_SPEED, lang=config.TTS_LANG
        )
        audio = np.asarray(samples, dtype=np.float32)
        if sr != config.HUD_RATE:
            audio = resample_poly(audio, config.HUD_RATE, sr).astype(np.float32)
        return audio

    def _synth_piper(self, text: str) -> np.ndarray:
        model = config.MODELS / f"{config.PIPER_VOICE}.onnx"
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            out = tmp.name
        try:
            proc = subprocess.run(
                [str(self._piper), "-m", str(model), "-f", out],
                input=text, text=True, capture_output=True, timeout=60,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr[-300:])
            with wave.open(out) as w:
                sr = w.getframerate()
                raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            audio = raw.astype(np.float32) / 32768.0
            if sr != config.HUD_RATE:
                audio = resample_poly(audio, config.HUD_RATE, sr).astype(np.float32)
            return audio
        finally:
            Path(out).unlink(missing_ok=True)

    def synth(self, text: str) -> np.ndarray:
        return self._synth_kokoro(text) if self.engine == "kokoro" else self._synth_piper(text)

    # ------------------------------------------------------------------- api
    def say(self, text: str):
        """Speak a complete string. Returns once the audio is queued."""
        text = clean(text)
        if not text:
            return
        generation = self.player.start_reply()
        try:
            self.player.write(self.synth(text), generation)
        except Exception as exc:
            print(f"[tts] synthesis failed: {exc}")

    def say_stream(self, deltas, on_spoken=None, stop=None):
        """Speak text as it arrives, one sentence at a time.

        on_spoken fires exactly once, when the first audio is queued, which is
        the moment the assistant has actually started talking. stop is polled so
        an interruption takes effect immediately rather than finishing the reply.
        """
        buffer = ""
        first = True
        # Claim a generation for this reply. Anything still being synthesised for
        # the previous one is refused from here on instead of arriving late.
        generation = self.player.start_reply()

        def emit_text(text: str) -> bool:
            nonlocal on_spoken, first
            if stop is not None and stop.is_set():
                return False
            try:
                audio = self.synth(text)
            except Exception as exc:
                print(f"[tts] synthesis failed: {exc}")
                return False
            if audio.size == 0:
                return False
            # this call is where synthesis spent seconds, so the reply may have
            # been interrupted while it ran. Refused audio must not count as
            # spoken either, or the state machine says "speaking" with silence.
            if not self.player.write(audio, generation):
                return False
            first = False
            if on_spoken is not None:
                on_spoken()
                on_spoken = None
            return True

        for delta in deltas:
            if stop is not None and stop.is_set():
                return
            buffer += delta
            ready, buffer = split_ready(buffer, first=first)
            if ready and (text := clean(ready)):
                emit_text(text)
        if tail := clean(buffer):
            emit_text(tail)
