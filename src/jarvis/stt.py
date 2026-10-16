"""Speech to text.

faster-whisper keeps the model resident, so a typical request costs a few
hundred milliseconds. Whisper hallucinates confidently on silence and noise,
so short or empty audio is rejected here rather than passed on.
"""

from __future__ import annotations

import numpy as np
from faster_whisper import WhisperModel

from . import config

# Things whisper emits into silence. Cheap to filter, embarrassing to speak aloud.
_HALLUCINATIONS = {
    "thank you.", "thanks for watching!", "thank you for watching.",
    "you", ".", "please subscribe.", "subscribe.", "[blank_audio]",
    "[silence]", "(silence)", "bye.", "okay.", "yeah.",
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

    def transcribe(self, audio: np.ndarray) -> str:
        """16 kHz float32 mono in, text out. Empty string when there is nothing to hear."""
        audio = np.asarray(audio, dtype=np.float32).flatten()
        if audio.size < MIN_SAMPLES:
            return ""

        peak = float(np.abs(audio).max())
        if peak < 1e-4:                       # pure digital silence
            return ""
        audio = audio / peak                  # whisper expects roughly unit scale

        segments, _ = self._model.transcribe(
            audio,
            beam_size=config.WHISPER_BEAM,
            language="en",
            condition_on_previous_text=False,
            initial_prompt=config.WHISPER_HINT,   # bias towards his actual vocabulary
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            temperature=[0.0, 0.2, 0.4],          # fall back if a pass looks bad
        )
        text = " ".join(s.text.strip() for s in segments).strip()

        if text.lower() in _HALLUCINATIONS:
            return ""
        return text
