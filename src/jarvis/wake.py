"""Wake word detection.

openWakeWord needs exactly 1280-sample (80 ms) frames, so this buffers
whatever the mic hands over and only scores when it has a full window.
"""

from __future__ import annotations

import os
import re

import numpy as np
import openwakeword
from openwakeword.model import Model

from . import config

EDGES = re.compile(r"^[^\w]+|[^\w]+$")        # punctuation stuck to a word


class WakeWord:
    def __init__(self):
        path = os.path.join(
            os.path.dirname(openwakeword.__file__),
            "resources", "models", config.WAKE_MODEL,
        )
        if not os.path.exists(path):
            raise FileNotFoundError(f"wake model missing: {path}")
        self._model = Model(wakeword_model_paths=[path])
        self._label = next(iter(self._model.models.keys()))
        self._buffer = np.zeros(0, dtype=np.int16)
        self._over = 0                        # consecutive frames above threshold
        self.last_score = 0.0                 # most recent score, for diagnostics

    def push(self, frame: np.ndarray, threshold: float | None = None) -> bool:
        """Feed one mic frame. True the moment the wake phrase is heard.

        threshold overrides the normal one, which matters while the assistant is
        speaking: its own voice is in the room and the bar should be higher.
        """
        limit = config.WAKE_THRESHOLD if threshold is None else threshold
        self._buffer = np.concatenate([self._buffer, frame.astype(np.int16)])
        woke = False
        while len(self._buffer) >= config.WAKE_FRAME:
            window, self._buffer = self._buffer[:config.WAKE_FRAME], self._buffer[config.WAKE_FRAME:]
            score = float(self._model.predict(window).get(self._label, 0.0))
            self.last_score = score
            if score >= limit:
                self._over += 1
                if self._over >= config.WAKE_CONSECUTIVE:
                    woke = True
            else:
                self._over = 0
        if woke:
            self.reset()
        return woke

    def reset(self):
        self._model.reset()
        self._buffer = np.zeros(0, dtype=np.int16)
        self._over = 0


class SpeechWake:
    """Wake phrase detection by understanding the words, not by a trained model.

    openWakeWord only ships a handful of pretrained phrases and none of them is
    a name of your choosing. Training a model is hours of work and a GPU. Since
    the recogniser is already loaded and running locally, it can simply read what
    was said and look for the phrase, which works for any phrase at all and lets
    the request arrive in the same breath as the wake word.
    """

    def __init__(self, phrase: str = config.WAKE_PHRASE,
                 aliases: tuple[str, ...] = config.WAKE_ALIASES):
        self.phrase = phrase.lower().strip()
        # the distinctive word, without the greeting: "maki" from "hey maki"
        parts = self.phrase.split()
        self.key = parts[-1] if parts else self.phrase
        self.aliases = tuple(a.strip().lower().rstrip("?,.!") for a in aliases)

    def match(self, text: str) -> tuple[bool, str]:
        """Return (heard the phrase, whatever came after it).

        The part that follows is taken from the original text, so a request
        keeps its capitalisation and its punctuation: "open github.com/Foo"
        stays itself rather than coming back lowercased.
        """
        if not text:
            return False, ""
        cleaned = text.strip()

        # the whole phrase, said plainly. matched on the original string so the
        # rest can be sliced out of it directly.
        found = re.search(re.escape(self.phrase), cleaned, re.IGNORECASE)
        if found:
            # lstrip, not strip: only the separator after the name is ours to
            # remove, the request keeps its own punctuation
            return True, cleaned[found.end():].lstrip(" ,.!?-")

        # any spelling the recogniser produced for the distinctive word
        words = cleaned.split()
        for i, word in enumerate(words):
            if EDGES.sub("", word).lower() in self.aliases or EDGES.sub("", word).lower() == self.key:
                return True, " ".join(words[i + 1:]).lstrip(" ,.!?-")
        return False, ""

    def strip_greeting(self, text: str) -> str:
        """Tidy a command that came in with the wake phrase."""
        for word in ("hey", "hi", "hello", "ok", "okay", "so", "please"):
            if text.lower().startswith(word + " "):
                text = text[len(word) + 1:]
        return text.lstrip(" ,.!?-").rstrip()
