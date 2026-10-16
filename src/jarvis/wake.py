"""Wake word detection.

openWakeWord needs exactly 1280-sample (80 ms) frames, so this buffers
whatever the mic hands over and only scores when it has a full window.
"""

from __future__ import annotations

import os

import numpy as np
import openwakeword
from openwakeword.model import Model

from . import config


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

    def push(self, frame: np.ndarray) -> bool:
        """Feed one mic frame. True the moment the wake phrase is heard."""
        self._buffer = np.concatenate([self._buffer, frame.astype(np.int16)])
        woke = False
        while len(self._buffer) >= config.WAKE_FRAME:
            window, self._buffer = self._buffer[:config.WAKE_FRAME], self._buffer[config.WAKE_FRAME:]
            score = float(self._model.predict(window).get(self._label, 0.0))
            if score >= config.WAKE_THRESHOLD:
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
