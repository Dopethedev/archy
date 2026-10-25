<p align="center">
  <img src="docs/banner.svg" alt="jarvis: a voice assistant that runs on your own machine" width="880">
</p>

<p align="center">
  <a href="https://github.com/Dopethedev/jarvis/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/Dopethedev/jarvis/tests.yml?branch=main&label=tests&style=for-the-badge&logo=githubactions&logoColor=white&color=4fe3ff&labelColor=05080c" alt="tests"></a>
  <a href="https://github.com/Dopethedev/jarvis/releases"><img src="https://img.shields.io/github/v/release/Dopethedev/jarvis?style=for-the-badge&label=release&logo=github&logoColor=white&color=4fe3ff&labelColor=05080c" alt="latest release"></a>
  <img src="https://img.shields.io/badge/licence-MIT-4fe3ff?style=for-the-badge&logo=opensourceinitiative&logoColor=white&labelColor=05080c" alt="MIT licence">
  <img src="https://img.shields.io/badge/python-3.12-4fe3ff?style=for-the-badge&logo=python&logoColor=white&labelColor=05080c" alt="python 3.12">
  <img src="https://img.shields.io/badge/linux%20%2B%20windows%20%2B%20macos-4fe3ff?style=for-the-badge&logo=linux&logoColor=white&labelColor=05080c" alt="Linux, Windows and macOS">
</p>

**A voice assistant that runs on your own machine.** Say the wake phrase, ask
something, hear the answer. It also reads your screen, sets reminders, briefs you,
sweeps the machine for anything odd, and drives a dashboard that changes mood by
voice.

Everything that hears and speaks is local: silero for voice activity,
faster-whisper for speech to text, kokoro for speech out. Only the thinking goes
to a model, through a local [`pi`](https://github.com/badlogic/pi-mono) process,
so the model is whichever one you point it at.

![the dashboard, standard theme](docs/theme-standard.png)

## Install

Needs [`pi`](https://github.com/badlogic/pi-mono) on `PATH`, a microphone, and
PipeWire on Linux.

```
git clone https://github.com/Dopethedev/jarvis jarvis && cd jarvis
uv sync
uv run python -m jarvis.fetch_models     # the voice models, 340 MB, all free
./run.sh                                 # say the wake phrase, then ask
```

Windows uses `run.cmd`; macOS uses the same `./run.sh`. The first run asks for
the Microphone permission and the first screen question asks for Screen
Recording, both under System Settings, Privacy and Security. A terminal that was
refused has to be started again after the tick.

`fetch_models` fetches the voice and its accents, copies the voice activity
detector out of the openWakeWord package, then opens each file the way the
assistant opens it, because a file that is present is not a file that loads.
`--check` reports without fetching, `--all` adds the fallback voice, `--force`
refetches. Whisper downloads its own model on the first question.

## What it does

- **Voice loop.** Wake phrase, request, answer, idle. The phrase is recognised
  rather than matched against a trained model, so any phrase works, and a request
  can arrive in the same breath as the wake word.
- **Interruption.** Talk over it, or say the phrase again, and it stops mid
  sentence. It also gives up on a slow model if you start talking.
- **Screen vision.** Ask about what is on screen and it looks.
- **Security sweep.** Processes, listeners, SSH, hosts file, world writable
  paths, user services, browser extensions, launchers, pending updates, and a
  ClamAV scan of the usual drop points. What is normal is reported too.
- **Briefing.** Time, date, weather, load, memory, uptime and the last sweep,
  gathered locally before the model is asked anything.
- **Reminders and timers.** Parsed locally, so they are confirmed in under a
  second and spoken when due. Clearing them says what went, with two minutes to
  undo.
- **Themes.** Three dashboard themes, switched by voice.
- **Memory.** Durable facts in a file you can read and edit, given to the model
  every turn so remembering costs no round trip.
- **A model chain.** Free relays die without warning. When the first stops
  answering the next takes over, and it says so.

The full list, including the limits, is in [FEATURES.md](FEATURES.md).

## Numbers from actual runs

| | |
|---|---|
| speech in, whisper `base.en` | 0.48 s for an utterance |
| speech out, kokoro | 1.10 s to make 4.10 s of speech |
| a reminder, a timer, a theme change | about 0.6 s, no model involved |
| resident memory while running | about 1.2 GB |
| the models it needs | 340 MB, free, no account |
| what leaves the machine | the text of what you said |

`base.en` over `small.en` because it measured 0.48 s against 1.36 s for the same
transcription, with no accuracy worth the wait.

## Themes

Switch by voice, or open the page with `?theme=combat`:

| standard | stealth | combat |
|---|---|---|
| ![standard](docs/theme-standard.png) | ![stealth](docs/theme-stealth.png) | ![combat](docs/theme-combat.png) |

## What leaves this machine

- **What you said**, as text, because that is the question. The audio never goes.
- **A picture of your screen**, only when you ask about it. Wayland and Windows
  keep it in memory; macOS writes one file and removes it in the same breath.
- **The local facts**, when you ask for a briefing or a sweep.
- **What it remembers about you**, since stored facts go to the model each turn.
- **A weather lookup**, asking open-meteo for your coordinates. No key, no account.

The default chain is free public relays, so a third party sees your text. Point
`pi` at a model you pay for, or one running here, and that stops being true. The
dashboard never leaves the machine: it listens on localhost and refuses anything
without its token.

## Requirements

- **Linux** with PipeWire and a microphone. Written on Arch with Hyprland; the
  voice loop is not tied to a desktop.
- **macOS**, sweep included. Screen questions use `screencapture`, which needs
  Screen Recording.
- **Windows** for everything except the sweep, which says so rather than reporting
  a clean machine. Screen capture uses Pillow.
- **Python 3.12**, which `uv` handles: ctranslate2 has no wheels past it.
- **grim and ffmpeg** for screen questions on Wayland.
- **clamav** for the virus scan part of the sweep. A check whose tool is missing
  is reported as skipped, never as clean.

## Your own settings

`config.py` holds every tunable and nothing personal. Copy the example:

```
cp config.local.example.json config.local.json
```

```json
{
  "USER_NAME": "your name",
  "CITY": "your city",
  "LATITUDE": 0.0,
  "LONGITUDE": 0.0,
  "WHISPER_HINT_EXTRA": "your name, your town, the programs you launch by voice",
  "WAKE_PHRASE": "hey jarvis",
  "WAKE_ALIASES": ["jarvis", "jervis"]
}
```

`WHISPER_HINT_EXTRA` and `WAKE_ALIASES` are worth filling in: the phrase is
recognised, not matched, and whisper will not spell an unseen name the same way
twice. The phrase and its last word are always honoured; the aliases are the
variants it invents on top. Anything left out falls back to a default, so no
location means the briefing says the weather is unavailable rather than guessing.
The file is gitignored.

## Running it

```
./run.sh                 voice loop, dashboard in its own window
./run.sh --no-hud        console only
./run.sh --say "hi"      one typed message, no microphone
./run.sh --mic-test      live microphone check: level, speech score, transcript
./run.sh --input Fifine  pick an input device by name
./tests/run_all.sh       every check that needs no microphone
```

## Layout

```
src/jarvis/
  config.py         every tunable, nothing else stores settings
  audio.py          microphone capture and speaker playback
  wake.py           wake phrase detection, by recognition or a trained model
  vad.py            is this frame speech, on onnxruntime directly
  stt.py            speech to text
  tts.py            text to speech
  brain.py          the pi process and its rpc protocol
  assistant.py      the state machine and the audio pump
  sweep.py          the security checks, for Linux and macOS
  sysinfo.py        cpu, memory, uptime, load and temperature, on three platforms
  briefing.py       time, weather, machine health, last sweep
  reminders.py      reminders and timers
  vision.py         screenshots for the model
  memory_store.py   durable facts, readable by hand
  hud.py            the dashboard server, the mic switch, the typed input
  mictest.py        the live microphone check
run.sh              the launcher for Linux and macOS
run.cmd             the launcher for Windows, where there is no bash
hud/                the dashboard page
docs/               the banner, the theme screenshots, and the scripts for both
tests/              one file per behaviour, all runnable without a microphone
```

`hud/` sits beside the package rather than inside it, because it is served as
static files: no build step, no framework. No email, calendar or music control,
because each needs an account and a credential; the seams are there, since a
request is either answered locally or handed to the model.

## Tests

```
./tests/run_all.sh
```

Each file is a behaviour, not a unit: the audio pump, the wake phrase shapes, the
sweep's browser checks, the reminder grammar, the model chain failing over, the
dashboard's own security. None need a microphone. The groups that need neither
hardware nor a model run on Linux, macOS and Windows, in the workflow behind the
badge at the top.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md): a test that cannot fail does not count,
numbers come from runs, failure paths first, personal settings never in a tracked
file. Built here with an AI assistant doing most of the typing, from my direction
and against this machine, which is why the history keeps what was wrong first.

## Licence

MIT, see [LICENSE](LICENSE). Not everything it uses is MIT:

- faster-whisper, kokoro-onnx, onnxruntime, sounddevice and Pillow are MIT;
  openwakeword is Apache-2.0; numpy is BSD; **soxr is LGPL-2.1-or-later**.
- **piper-tts is GPL-3.0-or-later** and is only the fallback voice, so `uv sync
  --extra fallback-voice` adds it and nothing else does. The code runs the
  `piper` program as a separate process rather than importing it, which is what
  keeps the two apart.
- The voice models keep the terms of whoever published them: Kokoro is
  Apache-2.0, Silero VAD is MIT, Whisper and the Piper voices have their own.
- The thinking is a model behind the relay you configure, with its own terms.
