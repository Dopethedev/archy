# jarvis

A voice assistant for this machine. Talk to it, it hears you, thinks with `pi`,
answers out loud, and can actually do things on the computer.

## What it is

A thin voice layer over `pi`. Everything that already works in `pi` (real shell
access, file tools, web search, its own memory) becomes something you can ask
for by voice.

    microphone -> openWakeWord ("hey jarvis")
               -> silero VAD (knows when you stopped talking)
               -> faster-whisper (speech to text, local)
               -> pi --mode rpc (the brain, one long-lived process)
               -> kokoro (text to speech, local)
               -> headset
               -> dashboard on localhost

## Why this shape

Mark LV was the previous attempt. It looked good and died constantly: a Windows
project that had never run on Wayland, a 10 minute session cap it ignored, a
socket that went half-open with no exception, two dozen patches to make it live
here. The visuals were the hard part and the part that broke everything else.

So this keeps the good half (voice in, voice out) and drops the fragile half
(the software rendered 3D head). The dashboard is a web page: cheap to draw,
impossible to crash the audio loop, and it can be restyled without touching
Python.

## What it does NOT do

- No wake word other than "hey jarvis".
- No cloud speech. Audio never leaves the machine; only the text goes to `pi`.
- No GPU use. Everything runs on the CPU.
- Not a general chat UI. It is tuned for short spoken answers, and the system
  prompt forces plain prose with no markdown, because markdown read aloud is
  noise.

## Layout

    src/jarvis/config.py      every tunable
    src/jarvis/audio.py       mic capture and speaker playback
    src/jarvis/wake.py        openWakeWord
    src/jarvis/stt.py         faster-whisper
    src/jarvis/tts.py         kokoro, with piper as fallback
    src/jarvis/brain.py       the pi rpc client
    src/jarvis/assistant.py   the state machine that ties it together
    src/jarvis/hud.py         dashboard server (stdlib only)
    hud/                      the dashboard page
    models/                   kokoro and piper voice files

## Decisions worth remembering

- **Python 3.12, not 3.14.** ctranslate2 publishes no wheels for 3.13+, so
  faster-whisper cannot install on a newer interpreter. 3.12 is the ceiling.
- **Everything audio goes through pipewire.** The headset's raw hardware only
  runs at 48 kHz, so asking PortAudio for `hw:0,0` at 16 kHz fails outright. The
  default pipewire device resamples happily.
- **The brain stays on the event loop.** pi's rpc queue belongs to the loop it was
  created on, so the brain is driven from the loop while transcription and
  speech synthesis (both blocking, both CPU-bound) run in threads.
- **Kokoro over piper.** piper is about 8x faster but plainly more robotic.
  Kokoro manages ~3x realtime here, which is fast enough, and speech starts at
  the first sentence rather than the whole reply.
- **Silence is filtered before it reaches whisper.** Whisper hallucinates
  confident sentences into silence ("Thank you."), and those would be spoken
  aloud as if you had said something.
