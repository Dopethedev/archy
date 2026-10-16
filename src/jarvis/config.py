"""Every tunable lives here, so behaviour can be changed without touching logic."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # ~/jarvis
MODELS = ROOT / "models"
HUD_DIR = ROOT / "hud"

# ---------------------------------------------------------------- audio
SAMPLE_RATE = 16000          # what whisper, silero and openWakeWord all want
HUD_RATE = 48000             # what the headset's hardware actually runs at
INPUT_BLOCK = 480            # 30 ms, silero's native frame
WAKE_FRAME = 1280            # 80 ms, openWakeWord's native frame

# ------------------------------------------------------------- wake word
WAKE_MODEL = "hey_jarvis_v0.1.onnx"
WAKE_THRESHOLD = 0.55
WAKE_CONSECUTIVE = 2         # this many frames over threshold before waking

# ------------------------------------------------------------ endpointing
SPEECH_ON = 0.50             # silero probability that counts as speech
END_SILENCE_MS = 850         # silence this long ends the utterance
MIN_SPEECH_MS = 250          # shorter than this is noise, not a request
MAX_UTTERANCE_S = 25         # hard stop, so a stuck mic cannot grow forever
PRE_ROLL_MS = 300            # audio kept from before speech was detected
LISTEN_TIMEOUT_S = 6.0       # wake word, then silence: give up and go back to idle

# ------------------------------------------------------- speech to text
WHISPER_MODEL = "base.en"
WHISPER_THREADS = 6

# ------------------------------------------------------- text to speech
TTS_ENGINE = "kokoro"        # "kokoro" (natural) or "piper" (faster)
TTS_VOICE = "bm_george"      # British male
TTS_LANG = "en-gb"
TTS_SPEED = 1.0
PIPER_VOICE = "en_GB-alan-medium"

# ----------------------------------------------------------------- brain
PI_BIN = "pi"
PI_THINKING = "off"          # voice wants speed, not deliberation
SESSION_DIR = ROOT / ".sessions"

VOICE_RULES = (
    "Your words are spoken aloud through a speaker. You are talking to Bishesh. "
    "Answer in one or two short sentences of plain prose. Never use markdown, "
    "code fences, bullet points, headings, emoji, or URLs, and never spell out a "
    "path unless asked; say the content in words. Do not describe your own "
    "reasoning or mention tools by name. If the user asks for something on this "
    "computer, do it with your tools and then say in one sentence what happened. "
    "If you do not know something, say so in one short sentence."
)

# ------------------------------------------------------------------- hud
HUD_HOST = "127.0.0.1"
HUD_PORT = 8731
