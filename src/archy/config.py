"""Every tunable lives here, so behaviour can be changed without touching logic.

Anything personal (your name, your city, words the recogniser should know) is
read from config.local.json if you create one; see config.local.example.json.
Nothing personal belongs in this file.
"""

from pathlib import Path
import json
import os

ROOT = Path(__file__).resolve().parents[2]          # ~/archy
LOCAL_CONFIG_FILE = Path(os.environ.get("ARCHY_LOCAL_CONFIG") or (ROOT / "config.local.json"))
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
WAKE_PHRASE = "hey archy"
# Extra spellings the recogniser may produce for the distinctive word, which is
# the last word of the phrase. The phrase itself and that word are always
# honoured; this is only for the variants a name collects, and it is worth
# filling in, because a name whisper has never seen is not spelled the same way
# twice. A name the recogniser has never seen needs the spelling it reaches for:
# asked for this one it wrote "Archie", which is why that ships here. So no
# install wakes on a spelling its own name does not have. Extend it for your own
# voice in config.local.json.
WAKE_ALIASES = ("archie",)
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
    "Archy, brief me, briefing, security sweep, stealth mode, combat mode, "
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

# The local chain: what to use when there is no internet at all. Empty means the
# feature is off, which is exactly the behaviour from before it existed, so an
# install that never fetches a local model never sees a difference. Filled in by
# config.local.json with a model the llama.cpp router serves.
PI_MODELS_OFFLINE = ()
# Which chain to use: "auto" follows the network, "online" never goes local, and
# "offline" never calls a relay. Offline-only is how the local path is tested for
# real without unplugging anything.
PI_CHAIN_MODE = "auto"

# ------------------------------------------------------- the local server
# llama.cpp's router, which pi speaks to natively. ARCHY starts it when the
# offline chain is needed and only stops one it started itself.
PI_LOCAL_SERVER_BIN = "llama-server"
PI_LOCAL_SERVER_URL = "http://127.0.0.1:8080"
PI_LOCAL_MODELS_DIR = Path.home() / "models"
# Confirmed on this machine: flash attention took prompt processing from 219 to
# 1274 tokens a second, which is the difference between a four second wait and a
# half second one on the first question. Generation is unchanged at about 48.
# The device has to be named: this machine has two Vulkan devices and the Intel
# one advertises more free memory because it borrows system RAM. `llama-server
# --list-devices` prints the names.
PI_LOCAL_DEVICE = "Vulkan0"
# The router runs with explicit loading, so a model has to be asked for. Loading a
# 4B off disk measured a few seconds here; the wait is here so a slow disk is a
# slow start rather than a lost question.
PI_LOCAL_LOAD_WAIT_S = 90.0
PI_LOCAL_SERVER_ARGS = (
    "--no-models-autoload", "--jinja", "-ngl", "999", "-c", "32768",
    "--flash-attn", "on",
    # No hidden reasoning. A small local model thinks before it speaks, and every
    # one of those tokens is silence in the user's ear: measured on a screen
    # question, 27.4 seconds to the first spoken word with thinking on, and the
    # same question answered in under a second with it off. The silence deadline
    # is 25 seconds, so a thinking local model was reported as a dropped
    # connection, which is exactly what it looked like from the outside.
    "--reasoning", "off",
)
# The local model is small and its prompt is paid for in seconds, so the voice
# path hands it this instead of pi's coding-assistant prompt: measured, 966
# tokens against 5099, and the difference is 0.8 seconds against 3.4 on a cold
# start. The relay keeps pi's own prompt, where the cost is not on this machine.
PI_LOCAL_SYSTEM_PROMPT = (
    "You are a voice assistant for a Linux desktop, answering out loud. "
    "Keep every answer to one or two short sentences of plain prose, with no "
    "markdown, no lists and no URLs. When asked to do something on this "
    "machine, do it with your tools, then say in one sentence what happened. "
    "If you do not know, say so in one short sentence."
)

# ------------------------------------------------------------ connectivity
# "There is no internet" and "the provider refused this model" are different
# failures with different answers, and one error string covers both until they
# are told apart (see net.py).
#
# The probe is a TCP connect to a few well-known addresses, not a DNS lookup and
# not a web request: DNS answers from a cache, and a captive portal answers a web
# request with a login page, and neither proves the relay is reachable. A turn
# that really happened outranks any probe, so being wrong here is temporary.
NET_PROBE_TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 53))
NET_PROBE_TIMEOUT_S = 1.5            # per target, tried in order until one connects
NET_CHECK_TTL_S = 20.0               # how long a verdict is trusted
NET_CHECK_MIN_INTERVAL_S = 5.0       # never probe more often than this, however often asked
NET_GREETING_WAIT_S = 2.0            # how long start-up waits for a verdict before speaking

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
SESSION_DIR = Path(os.environ.get("ARCHY_SESSION_DIR") or (ROOT / ".sessions"))
# overridable so the store can live elsewhere, and so tests can use their own
MEMORY_FILE = Path(os.environ.get("ARCHY_MEMORY_FILE") or (ROOT / "memory.json"))
# ------------------------------------------------------------------- permissions
# Off by default: with this off there is no gate, no policy file, and no
# enforcement, which is exactly how the assistant behaved before it existed. The
# gate itself is a pi extension, because pi can refuse a tool call in code and a
# system prompt cannot. See src/archy/gate/permission-gate.ts.
PERMISSIONS = False
PERMISSION_GATE = ROOT / "src" / "archy" / "gate" / "permission-gate.ts"
PERMISSION_POLICY = Path(os.environ.get("ARCHY_PERMISSION_POLICY")
                         or (STATE_DIR / "permission-policy.json"))
PERMISSION_PENDING = STATE_DIR / "permission-pending.json"
PERMISSION_APPROVALS = STATE_DIR / "permission-approvals.json"
PERMISSION_LOG = STATE_DIR / "permission-log.jsonl"
# Extra directories the assistant may read and write without asking. The repo and
# the state directory are always included; the home directory never is.
PERMISSION_ROOTS: tuple = ()
# Extra commands the gate may treat as read-only. It can widen the list, it cannot
# promote sudo, rm or a redirect into it.
PERMISSION_READONLY_COMMANDS: tuple = ()
# How long an approval lasts before it is stale, in seconds, and how long a
# blocked request stays on the waiting list.
PERMISSION_APPROVAL_TTL_S = 300.0

# ---------------------------------------------------------------------- privacy
# What may leave the machine when an online model is answering. Every default is
# the behaviour the assistant already had: a privacy setting that changes what
# happens on upgrade is not a setting. With the local model answering, none of it
# leaves the machine anyway, so these only govern remote requests. Enforced in
# code where the request is built, not in the prompt. See privacy.py.
PRIVACY_ONLINE_AI = True            # False means local only, whatever the chain says
PRIVACY_SHARE_MEMORY = True         # the stored facts go into the prompt
PRIVACY_SHARE_FILES = True          # file tools are usable at all
PRIVACY_SHARE_SCREENS = True        # a screenshot may be attached to a request
PRIVACY_SHARE_HISTORY = True        # earlier conversation is resumed
PRIVACY_KEEP_TRANSCRIPTS = True     # pi keeps its session files on disk

# ---------------------------------------------------------------------- dashboard
# The status panel: which model is answering, whether the network is up, what may
# leave the machine, what is waiting on approval. Off by default, because the
# dashboard before this showed the machine and nothing else, and a switch whose
# default changes what people already have is not a switch. Turning it on adds
# events to the same stream the dashboard already receives.
DASHBOARD_STATUS = False
STATUS_EVERY_TICKS = 3              # the panel refreshes every third stats tick
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

# The deeper checks, which are asked for rather than run every time: lynis takes
# about thirty seconds here and rkhunter minutes, and a sweep that makes the user
# wait two minutes in silence is one they stop asking for.
SWEEP_DEEP_WORDS = ("deep sweep", "deep scan", "deep check", "full sweep",
                    "thorough sweep", "deeper sweep", "deeper check", "lynis",
                    "rootkit check", "check for rootkits", "audit the system",
                    "audit this machine", "full audit")
SWEEP_LYNIS_TIMEOUT_S = 240.0
# Tests skipped by name, written into a lynis profile. NETW-2706 calls
# `resolvectl statistics`, which asks polkit for authentication on this machine: a
# deep sweep put a password dialog on the desktop every time it ran. The free lynis
# has no flag for this, so it is a profile file in the state directory.
SWEEP_LYNIS_SKIP_TESTS = ("NETW-2706",)
SWEEP_RKHUNTER_TIMEOUT_S = 300.0

# Dashboard themes, switched by voice: "archy, combat mode". The name has to
# ride along with the word mode or theme, so a passing mention of combat does
# not repaint the screen.
# What you say to reach a theme, and the phrases that mean "put it back". The
# names are the vocabulary the voice matcher listens for; the palettes live in
# the dashboard.
# --------------------------------------------------------------- the desktop
# What "focus the browser" means: the words people use for a program, mapped to the
# names the window itself carries. Kept here rather than in the matcher so it is
# yours to extend without touching code.
DESKTOP_ALIASES = {
    "browser": ("brave-browser", "firefox", "chromium", "google-chrome", "zen"),
    "chrome": ("brave-browser", "chromium", "google-chrome"),
    "code": ("code-oss", "code", "vscodium"),
    "editor": ("code-oss", "code", "vscodium", "kate", "gedit"),
    "terminal": ("ghostty", "alacritty", "kitty", "foot", "konsole", "wezterm"),
    "files": ("org.gnome.nautilus", "nautilus", "dolphin", "thunar", "pcmanfm"),
    "videos": ("mpv", "vlc", "totem", "haruna"),
    "music": ("spotify", "amberol", "lollypop"),
    "steam": ("steam", "steamwebhelper"),
    "discord": ("discord", "vesktop", "equibop"),
    "notes": ("obsidian", "logseq", "gnome-text-editor"),
    "chat": ("discord", "vesktop", "telegram-desktop", "signal"),
}
# The words that ask for a window to come to the front, and the name follows them.
DESKTOP_FOCUS_PHRASES = ("focus", "focus on", "switch to", "bring up", "bring back",
                         "bring me", "jump to", "put in front")
DESKTOP_LIST_PHRASES = ("what windows are open", "what's open", "what is open",
                        "which windows", "list the windows", "list windows",
                        "what do i have open", "what have i got open")
DESKTOP_CLOSE_PHRASES = ("close this window", "close the window",
                         "close the focused window")
# Words that start something a window is not, so "switch to workspace 3" is left to
# the model instead of being answered with a window focus that never happened.
DESKTOP_NOT_A_WINDOW = ("workspace", "monitor", "window", "tab", "screen", "desktop",
                        "output", "display")
DESKTOP_TIMEOUT_S = 5.0
DESKTOP_LIST_LIMIT = 5              # spoken, so short

# Remembering. These shapes are handled in code, without a model and without
# running memory_store as a command, so "remember that" is a fact stored now
# rather than a shell command that waits on the permission gate.
MEMORY_RECALL_PHRASES = (
    "what do you know about me", "what do you know about me so far",
    "what do you remember", "what have you remembered", "what do you have on me",
    "what have you got on me", "list what you know about me",
    "list what you remember", "read me your memory", "read out your memory",
    "tell me what you know about me", "tell me what you remember",
    "what do you know about me now",
)
# Anything starting with one of these is the same question with extra words on it.
MEMORY_RECALL_PREFIXES = ("what do you know about me", "what do you remember",
                          "list what you know", "list what you remember")

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
        "Window work is done for you in code: saying focus and an application, or "
        "asking what is open, is answered without a model. If you need a window "
        "action that is not one of those, the compositor here is niri: niri msg "
        "windows lists them and niri msg action takes focus-window --id, "
        "close-window, and workspace moves. Do not use the tools for other "
        "compositors: this machine does not run them and they fail silently. "
        "You have abilities that run without you: the dashboard has three themes "
        "the user changes by saying stealth mode, combat mode or standard mode "
        "(if asked to change it, say that saying it is what changes it); "
        "reminders and timers set by saying remind me or set a timer; a morning "
        "briefing when the user says brief me, with time, weather, machine health "
        "and the last security sweep; a security sweep when one is asked for, and a "
        "deeper audit of the machine when it is asked for as a deep sweep, which "
        "takes a minute; and "
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
BARGE_IN = True              # saying "hey archy" while it talks cuts it off
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


def _coerce(default, value):
    """One value from the local file, in the shape the setting it replaces has.

    A hand-edited file gets this wrong in one recurring way, and it is the one
    that says nothing about itself: a single string where a list was meant.
    `PI_MODELS` written as one model id was turned into a tuple of single
    characters by the spread, so the model chain became eleven models called "w",
    "a", "n", and every turn failed with a name nobody could explain. A string
    with commas in it is read as several items, because that is how people write
    a list by hand; a string without them is one item.

    Returns (ok, value). Not ok means the setting was left as it was, and why.
    """
    if isinstance(default, bool):
        if isinstance(value, bool):
            return True, value
        if isinstance(value, (int, float)):
            return True, value != 0
        if isinstance(value, str):
            spoken = value.strip().lower()
            if spoken in ("1", "true", "yes", "on"):
                return True, True
            if spoken in ("0", "false", "no", "off", ""):
                return True, False
        return False, "a yes or a no"
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, (int, float)):
            return True, int(value)
        if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
            return True, int(value.strip())
        return False, "a whole number"
    if isinstance(default, float):
        try:
            return True, float(value)
        except (TypeError, ValueError):
            return False, "a number"
    if isinstance(default, str):
        # A number in place of a word is a mistake, not a name: CITY read as "42"
        # would put a number where a place goes, in a sentence, out loud.
        return (True, value) if isinstance(value, str) else (False, "text")
    if isinstance(default, Path):
        if isinstance(value, (str, Path)):
            return True, Path(value)
        return False, "a path"
    if isinstance(default, tuple):
        if isinstance(value, str):
            items = [part.strip() for part in value.split(",")] if "," in value else [value]
            return True, tuple(part for part in items if part)
        if isinstance(value, (list, tuple)):
            return True, tuple(str(item) for item in value)
        return False, "a list of items, or one item"
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return False, "a group of settings"
        shaped = {}
        for name, inner in value.items():
            template = next(iter(default.values()), None) if default else None
            if isinstance(template, tuple) and not isinstance(inner, tuple):
                ok, fixed = _coerce((), inner)
                if not ok:
                    return False, f"a group whose {name!r} is a list of items"
                shaped[name] = fixed
            else:
                shaped[name] = inner
        return True, shaped
    if default is None:                 # a setting whose default is "not set"
        return (True, value) if isinstance(value, (str, int, float, bool)) \
            else (False, "a single value")
    return (True, value) if isinstance(value, type(default)) else (False, str(type(default)))


def apply_local_overrides(path: Path | None = None) -> list[str]:
    """Fold config.local.json in, so personal settings stay out of the repo.

    Copy config.local.example.json, fill in your name, city, coordinates and the
    words your recogniser should know, and it is read at import. Keys have to
    match the upper-case names above. Anything else, and anything written in a
    shape the setting cannot use, is reported and left alone rather than silently
    doing nothing or doing something surprising.

    Returns the lines it would print, so a test can read them.
    """
    path = LOCAL_CONFIG_FILE if path is None else path
    said: list[str] = []
    if not path.exists():
        return said
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        said.append(f"[config] ignoring {path.name}: {exc}")
        print(said[-1])
        return said
    if not isinstance(data, dict):
        said.append(f"[config] ignoring {path.name}: not an object")
        print(said[-1])
        return said
    for key, value in data.items():
        if key.startswith("_"):
            continue                    # a _comment field, by convention
        if not (key.isupper() and key in globals()):
            said.append(f"[config] {path.name}: unknown setting {key!r}")
            print(said[-1])
            continue
        default = globals()[key]
        if isinstance(value, type(default)) and not isinstance(default, dict):
            globals()[key] = value
            continue
        ok, fixed = _coerce(default, value)
        if not ok:
            said.append(f"[config] {key} is {value!r}, which is not {fixed}; "
                        f"using the default ({default!r})")
            print(said[-1])
            continue
        globals()[key] = fixed
    return said


apply_local_overrides()

# Built last, so it carries whatever name the local file set.
VOICE_RULES = _voice_rules()
