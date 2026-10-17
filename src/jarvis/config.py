"""Every tunable lives here, so behaviour can be changed without touching logic."""

from pathlib import Path
import os

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
# While it is waiting for a request it listens harder. The words after the wake
# phrase are the entire point, and a soft one must not be lost, so the gain goes
# up and the gate comes down. It is a window of seconds right after a beep,
# which is what makes being less conservative acceptable here.
AGC_TARGET_FOCUSED = 0.20
AGC_GATE_FACTOR_FOCUSED = 2.0
SPEECH_ON_FOCUSED = 0.22     # softer speech counts as speech while waiting

# ------------------------------------------------------------- wake word
# "speech" understands the phrase with whisper, so any phrase works and no
# training is needed. "model" uses openWakeWord's pretrained detector, which
# only exists for hey_jarvis, hey_mycroft, hey_marvin, alexa, timer, weather.
WAKE_MODE = "speech"
WAKE_PHRASE = "hey maki"
# Whisper will not spell an unfamiliar name the same way twice, so accept the
# shapes it actually produces rather than requiring one exact spelling.
WAKE_ALIASES = ("maki", "macki", "macky", "makki", "maky", "maki,",
                "machi", "magi", "maggi", "moki", "makahi", "maki?")
WAKE_IDLE_SILENCE_MS = 500   # silence that ends a wake-phrase utterance
WAKE_MAX_UTTERANCE_S = 10    # a wake phrase is short. Without this, continuous
                             # room noise becomes endless long recordings that all
                             # get transcribed, which saturates the CPU for nothing
WAKE_MODEL = "hey_jarvis_v0.1.onnx"
VAD_MODEL = MODELS / "silero_vad.onnx"   # silero VAD, run on onnxruntime directly
WAKE_THRESHOLD = 0.45        # other speech and noise measured at 0.000-0.020, so this
                             # is still far above anything that is not the phrase
WAKE_CONSECUTIVE = 2         # this many frames over threshold before waking

# ------------------------------------------------------------ endpointing
SPEECH_ON = 0.35             # silero probability that counts as speech
END_SILENCE_MS = 700         # silence this long ends the utterance
MIN_SPEECH_MS = 250          # shorter than this is noise, not a request
MAX_UTTERANCE_S = 20         # hard stop, so a stuck mic cannot grow forever
PRE_ROLL_MS = 300            # audio kept from before speech was detected
LISTEN_TIMEOUT_S = 12.0      # wake word, then silence: give up and go back to idle
# Once the user has been silent this long, the rest of the utterance is silence,
# so a transcript taken at this point is already complete. Starting it here runs
# it during the pause instead of after it, which is worth about a second.
SPECULATE_AT_MS = 300

# ------------------------------------------------------- speech to text
WHISPER_MODEL = "base.en"   # 0.48s against small.en's 1.36s, same accuracy
                            # once the vocabulary hint below is in place
WHISPER_THREADS = 6
WHISPER_BEAM = 5             # beam search instead of greedy: slower, clearly better
# 0.0 disables the temperature fallback entirely. The retry chain is what makes
# transcribing noise cost several times a successful transcription.
WHISPER_TEMPERATURE = 0.0
# Biases the recogniser towards words it would otherwise guess at. Whisper uses
# this as context, so proper nouns and jargon stop coming out as lookalikes.
WHISPER_HINT = (
    "Jarvis, Maki, Bishesh, Kathmandu, Nepal, Sober, Roblox, Hyprland, "
    "EndeavourOS, cpu, gpu, ram, terminal, wifi, bluetooth, screenshot, "
    "rkhunter, clamav."
)

# ------------------------------------------------------- text to speech
TTS_ENGINE = "kokoro"        # "kokoro" (natural) or "piper" (faster)
TTS_VOICE = "bm_george"      # British male
TTS_LANG = "en-gb"
TTS_SPEED = 1.0
PIPER_VOICE = "en_GB-alan-medium"

# ----------------------------------------------------------------- brain
PI_BIN = "pi"
# The brain as a chain: if the head dies (a free relay 404s, upstream fails),
# the assistant moves to the next and says so. All four verified answering
# through pi, and all four accept images, so screen vision survives a switch.
PI_MODELS = (
    "wandb/zai-org/GLM-5.3-Flash",        # current: fastest, sees images
    "crusoe/zai-org/GLM-5.3-Flash",       # the same model on another relay
    "wandb/moonshotai/Kimi-K2.6",
    "wandb/Qwen/Qwen3.8-27B",
)

# Small durable state: the last sweep's report, pending reminders. Lives under
# the project, recreated if missing, never in the session.
STATE_DIR = ROOT / ".state"
PI_THINKING = "off"          # voice wants speed, not deliberation
PI_CONTINUE = True           # resume the previous conversation after a restart
# The voice path needs no extension. donsetch spawns an MCP binary and does a
# handshake at session start, which costs a few hundred ms on the first turn
# and slightly more on every turn after it.
PI_EXTENSIONS = False
# overridable so tests cannot write into the live conversation: a test brain
# that shares this directory appends its own chatter to the real session
SESSION_DIR = Path(os.environ.get("JARVIS_SESSION_DIR") or (ROOT / ".sessions"))
# overridable so the store can live elsewhere, and so tests can use their own
MEMORY_FILE = Path(os.environ.get("JARVIS_MEMORY_FILE") or (ROOT / "memory.json"))
# If pi says nothing at all for this long the turn is abandoned and the model
# process is restarted. Without a deadline a hung brain leaves the assistant
# stuck in "thinking" forever, with no way back except a restart.
BRAIN_SILENCE_TIMEOUT_S = 25.0   # waiting a minute in silence feels like deafness

# Screen vision. A request containing one of these gets a screenshot attached.
# Deliberately eager: missing a "what am I looking at" is worse than one
# unnecessary capture. "window" is excluded on purpose, it belongs to too many
# ordinary requests.
SCREEN_WORDS = ("screen", "screenshot", "display", "monitor", "look at",
                "looking at", "see this", "what am i", "this page",
                "this game", "this app", "this website", "this video",
                "this tab", "this error", "this screen", "my desktop")

# Security sweep triggers. A scan of the machine on request; the clamav part is
# capped so it cannot quietly eat the evening.
SWEEP_WORDS = ("security sweep", "security scan", "security check", "virus scan",
               "virus check", "malware", "rootkit", "scan my", "sweep",
               "am i infected", "is my computer", "audit my", "check my computer",
               "any viruses")
SWEEP_CLAMAV_TIMEOUT_S = 120.0

# Dashboard themes, switched by voice: "maki, combat mode". The name has to
# ride along with the word mode or theme, so a passing mention of combat does
# not repaint the screen.
THEMES = {
    "standard": ("standard", "normal", "default", "regular", "restore the theme",
                 "reset the theme"),
    "stealth":  ("stealth",),
    "combat":   ("combat", "red alert", "battle mode"),
}

# The briefing: time, date, weather, machine health, the last sweep. Cheap
# local facts, gathered before the model is ever asked.
BRIEF_WORDS = ("brief me", "briefing", "good morning", "good afternoon",
               "good evening", "the weather", "weather today",
               "weather right now", "weather forecast", "today's date",
               "todays date", "what day is it", "what time is it",
               "how's the machine", "hows the machine", "system status",
               "status report", "am i good")
# Kathmandu, from the timezone this machine already keeps.
LATITUDE, LONGITUDE = 27.7172, 85.3240
WEATHER_CACHE_S = 1800.0

# Reminders and timers: parsed locally, kept in a small file, spoken when due.
REMINDER_CHECK_S = 5.0

VOICE_RULES = (
    "Your words are spoken aloud through a speaker. You are talking to Bishesh. "
    "Answer in one or two short sentences of plain prose. Never use markdown, "
    "code fences, bullet points, headings, emoji, or URLs, and never spell out a "
    "path unless asked; say the content in words. Do not describe your own "
    "reasoning or mention tools by name. If the user asks for something on this "
    "computer, do it with your tools and then say in one sentence what happened. "
    "To launch an application, use flatpak run with the app id for flatpak apps "
    "(sober is org.vinegarhq.Sober), otherwise gtk-launch with the desktop entry "
    "name. Run the launch command straight away rather than checking whether it "
    "or the machine state first: checking costs the user several seconds of "
    "silence, and a failed launch can be corrected afterwards. "
    "You have abilities that run without you: the dashboard has three themes he "
    "changes by saying stealth mode, combat mode or standard mode (if he asks "
    "you to change it, tell him to say that and it happens); reminders and "
    "timers he sets by saying remind me or set a timer; a morning briefing when "
    "he says brief me, with time, weather, machine health and the last security "
    "sweep; a security sweep when he asks for one; and you can see his screen "
    "when he asks about something on it. When he asks what you can do, list "
    "these honestly alongside your tools. "
    "If you do not know something, say so in one short sentence."
)

# ------------------------------------------------------------------- hud
HUD_HOST = "127.0.0.1"
HUD_PORT = 8731

# ------------------------------------------------------------ interaction
BARGE_IN = True              # saying "hey maki" while it talks cuts it off
BARGE_ON_SPEECH = True       # so does simply starting to talk over it
BARGE_ON_THINKING = True     # and the same while it is still thinking, so a long
                             # tool call can be redirected instead of waited out
BARGE_FRAMES = 5             # 150 ms of sustained speech counts as talking
BARGE_FRAMES_THINKING = 13   # while thinking, nothing is playing, so a stray
                             # 150 ms would silently swallow the answer. About
                             # 400 ms of deliberate speech instead.
BARGE_GRACE_MS = 500         # ignore the wake word this long after speech starts,
                             # so its own voice cannot trigger it
BARGE_HISTORY_MS = 600       # audio kept so the first word survives an interruption
WAKE_DURING_SPEECH = 0.60    # stricter than normal while it is speaking, but not so
                             # strict that interrupting becomes hard
