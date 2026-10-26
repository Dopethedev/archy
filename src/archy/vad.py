"""Voice activity detection: is this frame speech?

Silero's VAD run directly on onnxruntime. About forty lines of logic, which we
used to reach through openWakeWord: importing that package for this cost 159 MB
of memory and 0.74 s of start-up, because its package __init__ pulls in the
detector models and, through them, scikit-learn and scipy.

The model file is `silero_vad.onnx` from openWakeWord's resources, copied to
`models/` (silero-vad is MIT licensed). The state handling is deliberately the
same as the wrapper it replaces, so the scores agree exactly: 480-sample chunks,
an LSTM state carried between calls, and the mean score across the frame.
"""

from __future__ import annotations

import numpy as np
import onnxruntime as ort

from . import config

FRAME = 480                  # 30 ms at 16 kHz, what this model was trained on


class SpeechVAD:
    def __init__(self, model_path=None):
        path = model_path or config.VAD_MODEL
        if not path.exists():
            raise RuntimeError(
                f"the voice activity model is missing: {path}. It ships with "
                f"openWakeWord, copy silero_vad.onnx out of its resources/models."
            )
        # one thread each: this runs on every microphone frame, and letting it
        # spread across cores costs more than the frame is worth
        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._rate = np.array(config.SAMPLE_RATE, dtype=np.int64)
        self.reset()

    def reset(self):
        """Forget the audio heard so far, which also resets the LSTM state."""
        self._h = np.zeros((2, 1, 64), dtype=np.float32)
        self._c = np.zeros((2, 1, 64), dtype=np.float32)

    def speech(self, frame: np.ndarray, frame_size: int = FRAME) -> float:
        """Mean speech probability over one frame of 16 kHz int16 audio."""
        chunks = [frame[i:i + frame_size] for i in range(0, len(frame), frame_size)]
        if not chunks:
            return 0.0
        scores = []
        for chunk in chunks:
            audio = (chunk / 32767).astype(np.float32)[None, :]
            out, self._h, self._c = self._session.run(
                None, {"input": audio, "h": self._h, "c": self._c, "sr": self._rate}
            )
            scores.append(float(out[0][0]))
        return float(np.mean(scores))
