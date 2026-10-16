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
INPUT_DEVICE = None          # None = system default, or a device name substring

# The headset's own Auto Gain Control feeds the microphone back into its
# headphones, so it stays off and the gain is applied here instead.
HARDWARE_AGC = False
SOFT_AGC = True              # track the level and boost it in software
AGC_TARGET = 0.12            # speech RMS we aim for
AGC_MAX_GAIN = 80.0          # never boost more than this
AGC_FLOOR = 3e-5             # below this it is silence, so do not amplify it
AGC_GATE_FACTOR = 3.0        # only boost audio this far above the noise floor
MIC_GAIN = 1.0               # fixed extra gain, used when SOFT_AGC is off

# ------------------------------------------------------------- wake word
WAKE_MODEL = "hey_jarvis_v0.1.onnx"
WAKE_THRESHOLD = 0.55
WAKE_CONSECUTIVE = 2         # this many frames over threshold before waking

# ------------------------------------------------------------ endpointing
SPEECH_ON = 0.35             # silero probability that counts as speech
END_SILENCE_MS = 700         # silence this long ends the utterance
MIN_SPEECH_MS = 250          # shorter than this is noise, not a request
MAX_UTTERANCE_S = 25         # hard stop, so a stuck mic cannot grow forever
PRE_ROLL_MS = 300            # audio kept from before speech was detected
LISTEN_TIMEOUT_S = 12.0      # wake word, then silence: give up and go back to idle

# ------------------------------------------------------- speech to text
WHISPER_MODEL = "small.en"   # base.en is faster but mishears names and numbers
WHISPER_THREADS = 6
WHISPER_BEAM = 5             # beam search instead of greedy: slower, clearly better
# Biases the recogniser towards words it would otherwise guess at. Whisper uses
# this as context, so proper nouns and jargon stop coming out as lookalikes.
WHISPER_HINT = (
    "Jarvis, Bishesh, Sober, Roblox, Hyprland, EndeavourOS, cpu, gpu, ram, "
    "terminal, wifi, bluetooth, screenshot, rkhunter, clamav."
)

# ------------------------------------------------------- text to speech
TTS_ENGINE = "kokoro"        # "kokoro" (natural) or "piper" (faster)
TTS_VOICE = "bm_george"      # British male
TTS_LANG = "en-gb"
TTS_SPEED = 1.0
PIPER_VOICE = "en_GB-alan-medium"

# ----------------------------------------------------------------- brain
PI_BIN = "pi"
PI_THINKING = "off"          # voice wants speed, not deliberation
PI_CONTINUE = True           # resume the previous conversation after a restart
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

# ------------------------------------------------------------ interaction
BARGE_IN = True              # saying "hey jarvis" while it talks cuts it off
BARGE_GRACE_MS = 500         # ignore the wake word this long after speech starts,
                             # so its own voice cannot trigger it
WAKE_DURING_SPEECH = 0.70    # stricter threshold while it is speaking
