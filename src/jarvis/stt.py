"""Speech to text.

faster-whisper keeps the model resident, so a typical request costs a few
hundred milliseconds. Whisper hallucinates confidently on silence and noise,
so short or empty audio is rejected here rather than passed on.
"""

from __future__ import annotations

import threading

import numpy as np
from faster_whisper import WhisperModel

from . import config

# Things whisper emits into silence. Cheap to filter, embarrassing to speak aloud.
_HALLUCINATIONS = {
    "thank you.", "thanks for watching!", "thank you for watching.",
    "you", ".", "please subscribe.", "subscribe.", "[blank_audio]",
    "[silence]", "(silence)", "bye.", "okay.", "yeah.",
    "all right.", "alright.", "so.", "now.", "okay. all right.",
}

MIN_SAMPLES = int(0.2 * config.SAMPLE_RATE)


class Transcriber:
    def __init__(self):
        self._model = WhisperModel(
            config.WHISPER_MODEL,
            device="cpu",
            compute_type="int8",
            cpu_threads=config.WHISPER_THREADS,
        )
        # The model is not meant to be run concurrently, and several overlapping
        # transcriptions on a six-thread model thrash the whole machine instead
        # of finishing quicker. One at a time.
        self._lock = threading.Lock()

    def transcribe(self, audio: np.ndarray) -> str:
        """16 kHz float32 mono in, text out. Empty string when there is nothing to hear."""
        audio = np.asarray(audio, dtype=np.float32).flatten()
        if audio.size < MIN_SAMPLES:
            return ""

        peak = float(np.abs(audio).max())
        if peak < 1e-4:                       # pure digital silence
            return ""
        audio = audio / peak                  # whisper expects roughly unit scale

        with self._lock:
            segments, _ = self._model.transcribe(
                audio,
                beam_size=config.WHISPER_BEAM,
                language="en",
                condition_on_previous_text=False,
                initial_prompt=config.WHISPER_HINT,   # bias towards his real vocabulary
                no_speech_threshold=0.6,
                log_prob_threshold=-1.0,
                # No temperature fallback. Whisper retries a decode at a higher
                # temperature when the result looks bad, and on room noise every
                # attempt looks bad, so it walks the whole chain and pays a full
                # decode each time. Measured on this machine: 2.89s against 0.31s
                # for noise, and 4.12s against 0.90s for real speech, for the
                # same output. With beam search and no previous-text
                # conditioning there is little for the retries to rescue.
                temperature=config.WHISPER_TEMPERATURE,
            )
            text = " ".join(s.text.strip() for s in segments).strip()

        if text.lower() in _HALLUCINATIONS:
            return ""
        return text
