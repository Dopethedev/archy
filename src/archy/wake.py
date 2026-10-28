"""Wake word detection.

Two ways to hear the phrase. The default understands it from a transcript, so
nothing here runs. The other scores audio against a trained detector, and that
is what openWakeWord is for.

openWakeWord costs 159 MB of memory and 0.74 s of start-up, and it drags in
scikit-learn and scipy behind it. It used to be imported at the top of this
module, which meant the default path paid all of that for a detector it never
asks to run. The import lives inside WakeWord now, so the price falls only on
whoever chooses WAKE_MODE = "model".
"""

from __future__ import annotations

import os
import re

import numpy as np

from . import config

EDGES = re.compile(r"^[^\w]+|[^\w]+$")        # punctuation stuck to a word
HEAD_WORDS = 3   # how far into the utterance the full phrase may sit
GREETINGS = {"hey", "hi", "yo", "ok", "okay", "uh", "um", "ah", "eh", "so",
             "well", "alright"}   # a stray word in front of the name


class WakeWord:
    def __init__(self):
        try:
            import openwakeword
            from openwakeword.model import Model
        except ImportError as exc:                # pragma: no cover
            raise RuntimeError(
                "the model wake path needs openwakeword installed: uv add openwakeword"
            ) from exc
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
        # the distinctive word, without the greeting: "archy" from "hey archy"
        parts = self.phrase.split()
        self.key = parts[-1] if parts else self.phrase
        self.aliases = tuple(a.strip().lower().rstrip("?,.!") for a in aliases)

    def match(self, text: str) -> tuple[bool, str]:
        """Return (heard the phrase, whatever came after it).

        The part that follows is taken from the original text, so a request
        keeps its capitalisation and its punctuation: "open github.com/Foo"
        stays itself rather than coming back lowercased.

        The phrase is only honoured near the beginning of the utterance. You
        address the assistant first, so "hey archy, open sober" is a call, but
        "good idea, archy" spoken by a video on the desk is not, and without
        this rule the room answers itself.
        """
        if not text:
            return False, ""
        cleaned = text.strip()
        # the whole phrase, said plainly. matched on the original string so the
        # rest can be sliced out of it directly. it may sit a filler word deep:
        # "uh hey archy" is still a call.
        words = cleaned.split()
        head = " ".join(words[:HEAD_WORDS])
        found = re.search(re.escape(self.phrase), head, re.IGNORECASE)
        if found:
            # Count the words the match covers, then take the request from the
            # word list. Slicing the original string by the offset does not work:
            # the transcript is assembled from recogniser segments, and one empty
            # segment leaves a double space behind, which `head` has already had
            # collapsed. "hey  archy, open sober" sliced by a nine-character
            # offset gave back "y, open sober", and that went to the model.
            spoken = len(head[:found.end()].split())
            # lstrip, not strip: only the separator after the name is ours to
            # remove, the request keeps its own punctuation
            return True, " ".join(words[spoken:]).lstrip(" ,.!?-")

        # any spelling the recogniser produced for the distinctive word, but
        # only right at the front: the name is spoken first when you mean it.
        # "tell archy i said hello" is talk about the assistant, not to it.
        def stem(word: str) -> str:
            return EDGES.sub("", word).lower()

        if words:
            first = stem(words[0])
            if first in self.aliases or first == self.key:
                return True, " ".join(words[1:]).lstrip(" ,.!?-")
            if len(words) > 1 and first in GREETINGS:
                second = stem(words[1])
                if second in self.aliases or second == self.key:
                    return True, " ".join(words[2:]).lstrip(" ,.!?-")
        return False, ""

    def strip_greeting(self, text: str) -> str:
        """Tidy a command that came in with the wake phrase."""
        for word in ("hey", "hi", "hello", "ok", "okay", "so", "please"):
            if text.lower().startswith(word + " "):
                text = text[len(word) + 1:]
        return text.lstrip(" ,.!?-").rstrip()
