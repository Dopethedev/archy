"""Every tunable lives here, so behaviour can be changed without touching logic.

Anything personal (your name, your city, words the recogniser should know) is
read from config.local.json if you create one; see config.local.example.json.
Nothing personal belongs in this file.
"""

from pathlib import Path
import json
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
WAKE_PHRASE = "hey jarvis"
# Extra spellings the recogniser may produce for the distinctive word, which is
# the last word of the phrase. The phrase itself and that word are always
# honoured; this is only for the variants a name collects, and it is worth
# filling in, because a name whisper has never seen is not spelled the same way
# twice. Set it in config.local.json.
WAKE_ALIASES = ()
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
# Biases the recogniser towards words it would otherwise guess at, which is not
# a small effect. Whisper reads this as context for what it is about to hear, so
# its own command words belong here: "brief me", spoken at headset level,
# comes out as "brief man" or "breathe me" most of the time without it, and
# exactly right with it, measured over the phrase list at three levels. It also
# recovers "never mind", which arrives as "nevermind".
#
# Two words added to this list can remove others from the transcript, because
# the model treats the prompt as text that came before: "remind me" in the hint
# cost "remind me" from the front of a spoken reminder. So additions are priced
# by measuring the whole phrase list, not just the phrase being fixed.
#
# A machine's own names and programs go in WHISPER_HINT_EXTRA through
# config.local.json, which keeps this list the same on every install.
WHISPER_HINT = (
    "Jarvis, brief me, briefing, security sweep, stealth mode, combat mode, "
    "standard mode, screenshot, timer, reminder, microphone, weather, notice, "
    "Linux, cpu, gpu, ram, terminal, wifi, bluetooth, malware, rootkit"
)
WHISPER_HINT_EXTRA = ""      # a machine's own words: names, apps, place names


def whisper_hint() -> str:
    """What the recogniser is told to expect: the project's words, then yours."""
    extra = WHISPER_HINT_EXTRA.strip(" ,")
    return f"{WHISPER_HINT}, {extra}" if extra else WHISPER_HINT

# ------------------------------------------------------- text to speech
TTS_ENGINE = "kokoro"        # "kokoro" (natural) or "piper" (faster)
TTS_VOICE = "bf_emma"        # British female
TTS_LANG = "en-gb"
TTS_SPEED = 1.0
PIPER_VOICE = "en_GB-alba-medium"

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
# While a tool call is running, and for the rest of that turn, the process is
# doing more work than an ordinary question: a twenty second curl is a slow
# lookup rather than a dead model, and the answer after it is a second pass with
# the results in the context, whose first token is measured in tens of seconds.
# Killing either at the plain silence deadline reported a healthy model as a
# dropped connection and threw the lookup away. A turn with no tools keeps the
# short deadline, so a genuine hang is still noticed quickly.
BRAIN_TOOL_TIMEOUT_S = 90.0
# How many lookups a spoken turn may make before it is told to answer with what
# it has. Measured on a real request, "search the web for the newest RAM prices"
# spent fifty four seconds over about twenty curl calls fighting Amazon's bot
# wall, when the answer only needed the news it already had. The instruction to
# answer after one or two lookups was already in the voice rules and was ignored,
# so this is enforced rather than asked for: at this many tool calls the turn is
# steered, which pi delivers after the running tool finishes.
BRAIN_MAX_TOOLS = 8
# And a limit on how long a turn may spend looking things up. The count alone was
# not enough: measured on the same question, two lookups took fifty three seconds
# because each one ran to its own twenty five second timeout, so a turn is also
# steered to answer once it has been in tools this long.
BRAIN_TURN_LOOKUP_S = 20.0
# process is restarted. Without a deadline a hung brain leaves the assistant
# stuck in "thinking" forever, with no way back except a restart.
BRAIN_SILENCE_TIMEOUT_S = 25.0   # waiting a minute in silence feels like deafness
# A silent model gets this many tries before the chain moves on. One, because a
# slow tool run looks exactly like a dead relay from here; not more, because a
# relay that accepts a connection and then says nothing would otherwise be
# retried forever while every answer is "my connection dropped".
MODEL_HANG_LIMIT = 2
# The conversation file grows for as long as the assistant lives, and every
# start pays for it in load time. Past this, it is moved aside and a fresh
# conversation begins; pi compacts the context itself, but not the file. Durable
# facts live in the memory store and are untouched by this.
SESSION_ARCHIVE_MB = 8.0

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
# The scan is bounded twice over, because a scratch directory on a working
# machine is gigabytes of build litter: measured here, /tmp held 9,859 files and
# 1.2 GB, so scanning "everything" spent two minutes of CPU and was cut off part
# way every single sweep, which reads as clean without having covered much. The
# newest files are the ones that matter, a fresh dropper is a recent file, and
# the report says exactly what was and was not covered.
SWEEP_CLAMAV_TIMEOUT_S = 45.0
SWEEP_CLAMAV_MAX_FILES = 400
SWEEP_CLAMAV_ENUMERATE = 20000      # stop listing after this many, pathological dirs

# Dashboard themes, switched by voice: "jarvis, combat mode". The name has to
# ride along with the word mode or theme, so a passing mention of combat does
# not repaint the screen.
# What you say to reach a theme, and the phrases that mean "put it back". The
# names are the vocabulary the voice matcher listens for; the palettes live in
# the dashboard.
THEMES = {
    "standard": ("standard", "normal", "default", "regular"),
    "stealth":  ("stealth",),
    "combat":   ("combat", "red alert", "battle"),
}
THEME_RESET_PHRASES = ("restore the theme", "reset the theme", "restore theme",
                       "reset theme")

# The briefing: time, date, weather, machine health, the last sweep. Cheap
# local facts, gathered before the model is ever asked.
BRIEF_WORDS = ("brief me", "briefing", "good morning", "good afternoon",
               "good evening", "the weather", "weather today",
               "weather right now", "weather forecast", "today's date",
               "todays date", "what day is it", "what time is it",
               "how's the machine", "hows the machine", "system status",
               "status report", "am i good")
# The spelling of the wake phrase is this project's own business, but the name
# it calls you is yours. Used in the system prompt and in spoken confirmations.
USER_NAME = "the user"

# The briefing's weather line. open-meteo needs no key, only a location; with no
# coordinates it says the weather is unavailable instead of guessing.
CITY = ""
LATITUDE = None
LONGITUDE = None
WEATHER_CACHE_S = 1800.0

# A microphone delivering exact zeros, every sample, is switched off, has its
# boom up, or has stalled. This many seconds of it and the assistant says so:
# otherwise it waits to be spoken to forever and looks like it is ignoring you.
MIC_DEAF_S = 20.0

# Reminders and timers: parsed locally, kept in a small file, spoken when due.
# The check itself is a read of a small JSON file, measured in microseconds, so
# it runs often enough that "in twelve seconds" means twelve: at five seconds a
# twelve second timer was spoken sixteen seconds later, which is a promise the
# assistant visibly fails to keep.
REMINDER_CHECK_S = 1.0
# How long a clearance can be undone. Long enough to notice a mishearing, short
# enough that "undo" later is not a surprise.
REMINDER_UNDO_S = 120.0

def _voice_rules() -> str:
    """The spoken-conversation rules. A function so it is built after the local
    overlay has had its say about the user's name."""
    return (
        "Your words are spoken aloud through a speaker. You are talking to "
        f"{USER_NAME}. "
        "Answer in one or two short sentences of plain prose. Never use markdown, "
        "code fences, bullet points, headings, emoji, or URLs, and never spell out "
        "a path unless asked; say the content in words. Do not describe your own "
        "reasoning or mention tools by name. If the user asks for something on "
        "this computer, do it with your tools and then say in one sentence what "
        "happened. To launch an application, use flatpak run with the app id for "
        "flatpak apps, otherwise gtk-launch with the desktop entry name. Run the "
        "launch command straight away rather than checking whether it works or "
        "the machine state first: checking costs the user several seconds of "
        "silence, and a failed launch can be corrected afterwards. "
        "You have abilities that run without you: the dashboard has three themes "
        "the user changes by saying stealth mode, combat mode or standard mode "
        "(if asked to change it, say that saying it is what changes it); "
        "reminders and timers set by saying remind me or set a timer; a morning "
        "briefing when the user says brief me, with time, weather, machine health "
        "and the last security sweep; a security sweep when one is asked for; and "
        "you can see the screen when the user asks about something on it. When "
        "asked what you can do, list these honestly alongside your tools. "
        "Looking something up costs the user silence, so do it in one or two "
        "lookups and then answer: a price question that kept trying for five "
        "lookups and thirty five seconds ended with no answer at all, and an "
        "answer with only the part you know beats that. When you fetch a page, "
        "give it eight seconds at most, because one slow shop page that runs to "
        "its twenty five second timeout is most of a minute of silence on its "
        "own: never wait longer than that for a page. Shop sites block this "
        "kind of request and will keep blocking it, so if one refuses, answer "
        "from what you have rather than trying another shop. "
        "If you do not know something, say so in one short sentence."
    )


# ------------------------------------------------------------------- hud
HUD_HOST = "127.0.0.1"
HUD_PORT = 8731

# ------------------------------------------------------------ interaction
BARGE_IN = True              # saying "hey jarvis" while it talks cuts it off
BARGE_ON_SPEECH = True       # so does simply starting to talk over it
BARGE_ON_THINKING = True     # and the same while it is still thinking, so a long
                             # tool call can be redirected instead of waited out
# 300 ms of sustained speech counts as talking over it. It was 150 ms, and on a
# machine where a video often plays aloud that is short enough for a single blip
# to cut off an answer: one did, at 31 seconds into a lookup answer, and the
# utterance that caused it was then discarded as too short to be a request, so
# the answer was simply lost. A person talking over it still says whole words.
BARGE_FRAMES = 10
BARGE_FRAMES_THINKING = 13   # while thinking, nothing is playing, so a stray
                             # 150 ms would silently swallow the answer. About
                             # 400 ms of deliberate speech instead.
BARGE_GRACE_MS = 500         # ignore the wake word this long after speech starts,
                             # so its own voice cannot trigger it
BARGE_HISTORY_MS = 600       # audio kept so the first word survives an interruption
WAKE_DURING_SPEECH = 0.60    # stricter than normal while it is speaking, but not so
                             # strict that interrupting becomes hard


def _apply_local_overrides() -> None:
    """Fold config.local.json in, so personal settings stay out of the repo.

    Copy config.local.example.json, fill in your name, city, coordinates and the
    words your recogniser should know, and it is read at import. Keys have to
    match the upper-case names above; anything else is ignored with a warning
    rather than silently doing nothing.
    """
    path = ROOT / "config.local.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print(f"[config] ignoring config.local.json: {exc}")
        return
    if not isinstance(data, dict):
        print("[config] ignoring config.local.json: not an object")
        return
    for key, value in data.items():
        if key.startswith("_"):
            continue                    # a _comment field, by convention
        if not (key.isupper() and key in globals()):
            print(f"[config] config.local.json: unknown setting {key!r}")
            continue
        globals()[key] = value


_apply_local_overrides()

# Built last, so it carries whatever name the local file set.
VOICE_RULES = _voice_rules()
