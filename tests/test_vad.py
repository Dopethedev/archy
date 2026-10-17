"""Our own VAD must score audio exactly like the one it replaced.

The VAD decides what counts as speech, so a quiet change of scoring would move
every threshold in the assistant with it. This feeds the same audio through both
implementations and requires identical numbers, then checks the two things the
assistant actually depends on: speech scores high, silence and noise score low.
"""
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np  # noqa: E402
from jarvis import config  # noqa: E402
from jarvis.vad import SpeechVAD  # noqa: E402

failures = 0
def check(label, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:52s} {detail}")


def speech_16k(seconds: float = 2.0) -> np.ndarray:
    """Real speech, synthesised, because synthesised speech is still speech."""
    from kokoro_onnx import Kokoro
    from jarvis.audio import resample
    kokoro = Kokoro(str(config.MODELS / "kokoro-v1.0.onnx"),
                    str(config.MODELS / "voices-v1.0.bin"))
    samples, sr = kokoro.create(
        "Systems online. Your cpu usage is twelve percent, and the terminal is open now.",
        voice=config.TTS_VOICE, speed=config.TTS_SPEED, lang=config.TTS_LANG)
    audio = resample(np.asarray(samples, dtype=np.float32), sr, config.SAMPLE_RATE)
    audio = np.clip(audio * 32767, -32768, 32767).astype(np.int16)
    # pad or trim to the requested length
    want = int(config.SAMPLE_RATE * seconds)
    if len(audio) < want:
        audio = np.concatenate([audio, np.zeros(want - len(audio), np.int16)])
    return audio[:want]


rng = np.random.default_rng(5)
speech = speech_16k()
silence = np.zeros_like(speech)
noise = (rng.standard_normal(speech.size) * 120).astype(np.int16)   # room level

ours = SpeechVAD()

# ---------------------------------------------------------------- agreement
try:
    from openwakeword.vad import VAD as TheirVAD
except ImportError:
    TheirVAD = None

if TheirVAD is None:
    print("  [SKIP] openwakeword is not installed, so nothing to compare against")
else:
    theirs = TheirVAD()
    for label, audio in [("speech", speech), ("silence", silence), ("noise", noise)]:
        frame = config.INPUT_BLOCK
        diffs = []
        for i in range(0, len(audio) - frame, frame):
            chunk = audio[i:i + frame]
            diffs.append(abs(theirs.predict(chunk, frame_size=frame)
                             - ours.speech(chunk, frame_size=frame)))
        worst = max(diffs)
        check(f"identical scores on {label}", worst < 1e-5,
              f"largest difference {worst:.2e} over {len(diffs)} frames")


# ------------------------------------------------------------- and it discriminates
def speechiness(audio: np.ndarray) -> float:
    """What the assistant sees: the score of one 30 ms frame of audio."""
    vad = SpeechVAD()
    scores = []
    frame = config.INPUT_BLOCK
    for i in range(0, min(len(audio), config.SAMPLE_RATE) - frame, frame):
        scores.append(vad.speech(audio[i:i + frame], frame_size=frame))
    return float(np.mean(scores))


s_score = speechiness(speech)
q_score = speechiness(silence)
n_score = speechiness(noise)
check("speech is heard as speech", s_score > config.SPEECH_ON,
      f"{s_score:.2f} against a threshold of {config.SPEECH_ON}")
check("silence is not heard as speech", q_score < 0.1, f"{q_score:.3f}")
check("room noise is not heard as speech", n_score < config.SPEECH_ON * 0.5,
      f"{n_score:.3f} against a threshold of {config.SPEECH_ON}")

# the state has to reset cleanly, or a frame of speech bleeds into what follows
vad = SpeechVAD()
frame = np.zeros(config.INPUT_BLOCK, np.int16)
after_speech = vad.speech(speech[:config.INPUT_BLOCK * 20], frame_size=config.INPUT_BLOCK)
vad.reset()
check("resetting forgets the speech before it",
      vad.speech(frame, frame_size=config.INPUT_BLOCK) < 0.1,
      f"{after_speech:.2f} before the reset, "
      f"{vad.speech(frame, frame_size=config.INPUT_BLOCK):.3f} after")

# the cost of it, since this runs on every microphone frame
vad = SpeechVAD()
chunk = speech[:config.INPUT_BLOCK]
vad.speech(chunk)
t0 = time.perf_counter()
for _ in range(200):
    vad.speech(chunk)
per_frame = (time.perf_counter() - t0) / 200
check("fast enough for every frame at 30 ms", per_frame < 0.005,
      f"{per_frame * 1000:.2f}ms per frame, one frame is 30ms of audio")

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
