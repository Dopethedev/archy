# jarvis

A local voice assistant for Linux. Say the wake phrase, ask something, hear the
answer, and control the machine by voice.

```
microphone -> silero VAD -> faster-whisper -> wake phrase check
           -> pi --mode rpc (the brain)
           -> kokoro voice -> headphones
           -> dashboard at http://127.0.0.1:8731
```

Everything that hears and speaks runs locally. The thinking is done by a `pi`
process, which is where a model connection is used.

## Running it

```
./run.sh                 voice loop, dashboard opens in its own window
./run.sh --no-hud        console only
./run.sh --say "hi"      one typed message, no microphone
./run.sh --mic-test      live microphone check: level, speech score, transcript
./tests/run_all.sh       every check that needs no microphone
```

The wake phrase is **"hey maki"**. It is understood by the recogniser rather
than matched by a trained model, which is why an arbitrary phrase works and why
a request can arrive in the same breath ("hey maki, open sober").

Four states, one audio loop: `idle` waits for the phrase, `listening` records
the request, `thinking` waits on the model, `speaking` plays the reply. Talking
over it, or saying the phrase again, cuts it off.

## Requirements

- Python 3.12 (`uv` handles this). ctranslate2 has no wheels past 3.12.
- A microphone and headphones. Pipewire is assumed.
- The `pi` coding agent on `PATH`.

## Layout

```
src/jarvis/
  config.py         every tunable, nothing else stores settings
  audio.py          microphone capture and speaker playback
  wake.py           wake phrase detection (understood, and the trained model)
  stt.py            speech to text
  tts.py            text to speech
  brain.py          the pi process and its rpc protocol
  assistant.py      the state machine and the audio pump
  vad.py            is this frame speech, on onnxruntime directly
  hud.py            the dashboard server
  memory_store.py   durable facts, readable and editable by hand
  mictest.py        the live microphone check
hud/                the dashboard page
models/             voice and wake models (not committed)
```

Models are not in the repository. Four files, all free downloads:

| file | where |
|------|-------|
| `kokoro-v1.0.fp16.onnx` | github.com/thewh1teagle/kokoro-onnx, release `model-files-v1.1` |
| `voices-v1.0.bin` | same release |
| `en_GB-alan-medium.onnx` + `.json` | piper voices, for the fallback engine |
| `silero_vad.onnx` | copy out of openWakeWord's `resources/models`, or silero-vad's own release |

Whisper downloads itself into the huggingface cache the first time it runs.

`./run.sh --input Fifine` picks a different input device by name. Run
`./tests/run_all.sh` before trusting a change.
