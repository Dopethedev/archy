# status

## Version
0.1.0 — working. Verified live: wake word, transcription, a real tool call, and
a spoken answer.

## What works now
- Wake word: "hey jarvis". Fires on the phrase at 0.998, quiet at 0.000-0.020
  on other speech, silence and noise.
- Speech to text: faster-whisper **small.en** with beam search and a vocabulary
  hint covering his actual words. Correct even when the audio is degraded to
  this microphone's real level (about nine bits of resolution), in ~1 s.
- Brain: `pi --mode rpc`, one long-lived process. 1.2 s to first token cold,
  0.6 s for a normal turn, ~2.3 s when it runs a tool.
- **Memory survives restarts.** `--continue` resumes the previous session, so
  it still knows what you told it before it was restarted.
- Voice: kokoro `bm_george` (British male), ~3x realtime, starts speaking at the
  first sentence while the model is still thinking.
- **Interruption.** Say "hey jarvis" while it is talking and it stops, aborts
  the model and listens. Guarded three ways: a 500 ms grace period at the start
  so its own opening word cannot trigger it, a stricter threshold (0.70) while
  speaking, and a fresh cancellation token per turn so an interrupted turn
  cannot clear the state its replacement has moved into.
- Turns end after 700 ms of silence.
- Dashboard: stdlib http server, loopback only, SSE. Orb reacts to real input
  and output levels, activity log, CPU/RAM/temp. Typing in the box is handled
  exactly like speech.

## The microphone, since it cost the most time
- The headset's own Auto Gain Control is what makes the microphone audible at
  all, and it is also what feeds the mic back into the headphones. It stays off
  and the gain is applied in software instead.
- The raw headset mic is very weak (about 0.001 RMS), so software gain has to be
  large. A naive AGC therefore amplified the room itself to speech level, and
  the VAD then either fired on nothing or never ended a turn. Hence the noise
  gate: audio below three times the measured floor passes through untouched.
- Two silent faults found by testing rather than observation: MicCapture handed
  out views into PortAudio's reused buffer (which turned a 20 second recording
  into a constant tone), and the AGC had no gate.

## Not verified
- Endpointing against real speech habits. 700 ms of silence ends a turn.
- Long-run stability. Every session so far has been minutes, not hours.
- Interruption on real speech: the logic is tested, but whether its own voice
  through the room trips the wake word has only been reasoned about, not heard.

## Known limits
- Answers depend on the free Gemini quota through `pi`. When that runs dry the
  local speech parts keep working and only the thinking suffers.
- The headset microphone is the weakest link: its raw output is very quiet, so
  software gain has to be large. A better microphone would improve recognition
  more than any further tuning.
- The dashboard is not authenticated. It binds to 127.0.0.1 only.

## Environment
- EndeavourOS, Hyprland/Wayland. Python 3.12 via uv (3.14 cannot build
  ctranslate2). i5-12400, 12 threads, CPU only.
- Mic and output: Fantech Leviosa through pipewire. The headset hardware only
  runs at 48 kHz, so every capture goes through pipewire's resampler; asking for
  the raw device at 16 kHz fails outright.
- Models: `models/kokoro-v1.0.onnx` (311 MB) + `voices-v1.0.bin` (27 MB),
  `models/en_GB-alan-medium.onnx` (61 MB) as the piper fallback.

## How to run
    ./run.sh                 voice loop + dashboard window
    ./run.sh --no-hud        console only
    ./run.sh --say "hi"      one typed message, no mic
    ./run.sh --mic-test      live mic check: level, speech score, what it heard
    ./run.sh --mic-test --input Fifine    check the other microphone
    ./tests/run_all.sh       every check that needs no microphone

## Next
- The email and security-sweep tools that were wanted at the start.
- Persist the wake word setting and other preferences across restarts.
