# status

## Version
0.1.0 — first working build.

## What works now
- Wake word: "hey jarvis", openWakeWord, verified 0.998 on the phrase and
  0.000-0.020 on other speech, silence and noise.
- Speech to text: faster-whisper base.en, local, ~11x realtime (0.33 s to
  transcribe 3.7 s of audio). Silence and known hallucinations are dropped.
- Brain: `pi --mode rpc`, one long-lived process. 1.5 s to first token on a cold
  start, 0.8 s for a normal turn, ~3 s when it runs a tool.
- Voice: kokoro `bm_george` (British male), ~3x realtime, starts speaking at the
  first sentence. piper `en_GB-alan` is the fallback if kokoro models are absent.
- Audio: mic and speaker both through pipewire. Verified capture and playback;
  the headset is closed-back so it cannot be loopback-tested.
- Dashboard: stdlib http server, loopback only, SSE. Orb reacts to real input
  and output levels, rotating rings, ripples on wake, activity log, CPU/RAM/temp.
  Type into the box and it is handled exactly like speech.
- Typed path end to end: `uv run python -m jarvis --say "hello"` produced a real
  spoken reply through the speakers.

## Not yet verified
- The full spoken loop with a human voice. Every piece is tested in isolation and
  the pipeline runs headless, but nobody has said "hey jarvis" into the mic and
  been answered by this build yet.
- Endpointing timings (850 ms of silence to end a turn) are untuned against real
  speech habits.
- Barge-in: it cannot currently be interrupted while speaking.

## Known limits
- Answers depend on the free Gemini quota through `pi`. When that runs dry,
  replies degrade but the local speech parts keep working.
- Whisper base.en occasionally mishears names; small.en is the upgrade if that
  annoys (about 2x slower, still realtime).
- Session memory resets on restart; `--session-dir` is used but `--continue` is
  not wired up yet.

## Environment
- EndeavourOS, Hyprland/Wayland. Python 3.12.16 via uv (3.14 cannot build
  ctranslate2). i5-12400, 12 threads, CPU only.
- Mic: Fantech Leviosa (pipewire default source). Output: Fantech Leviosa,
  unmuted, 80%.
- Models: `models/kokoro-v1.0.onnx` (311 MB) + `voices-v1.0.bin` (27 MB),
  `models/en_GB-alan-medium.onnx` (61 MB).

## How to run
    ./run.sh                 voice loop + dashboard window
    ./run.sh --no-hud        console only
    ./run.sh --say "hi"      one typed message, no mic

## Next
- Live mic test with a human voice.
- Tune the endpointing.
- Barge-in while it is speaking.
- Persist conversation across restarts.
