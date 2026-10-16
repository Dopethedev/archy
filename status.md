# status

## State
Working and in use. A turn takes **1.7 s** from the end of speech to the first
spoken word. Verified live: wake phrase, transcription, a real shell command,
and a spoken answer.

Run it with `./run.sh`. Wake phrase is **"hey maki"** (just "maki" also works).

## How it works end to end
    microphone -> silero VAD -> faster-whisper -> wake phrase check
               -> pi --mode rpc (the brain, one long-lived process)
               -> kokoro voice -> headphones
               -> dashboard on 127.0.0.1:8731

Four states: `idle` (waiting for the phrase), `listening` (armed, after the
phrase), `thinking`, `speaking`. One audio pump drives all of it and never
blocks: transcription, the model and the voice all run off the loop, so the
microphone queue keeps draining in every state.

## Current settings that matter (src/jarvis/config.py holds all of them)
- `WAKE_MODE = "speech"`, `WAKE_PHRASE = "hey maki"`. The phrase is understood by
  the recogniser, not matched by a trained model, because none exists for an
  arbitrary name. This also lets "hey maki, open sober" work in one breath.
- `WHISPER_MODEL = "base.en"`, beam 5, plus `WHISPER_HINT` listing his words.
  Measured 0.48 s against small.en's 1.36 s with identical accuracy: the hint is
  what fixed names like Sober and Kathmandu, not the larger model.
- `END_SILENCE_MS = 700`, `WAKE_IDLE_SILENCE_MS = 500`, `SPECULATE_AT_MS = 300`.
- Transcription is serialised by a lock. `WAKE_MAX_UTTERANCE_S = 10`,
  `MAX_UTTERANCE_S = 20`.
- `HARDWARE_AGC = False`, `SOFT_AGC = True` with a noise gate.
- `PI_CONTINUE = True` (memory survives restarts), `PI_EXTENSIONS = False`,
  `PI_THINKING = "off"`.
- Barge-in: `BARGE_ON_SPEECH`, 150 ms of speech cuts it off, `BARGE_GRACE_MS = 500`,
  and the audio that triggered it is kept so the first word is not swallowed.

## The microphone (this cost the most time)
- The headset's own Auto Gain Control makes the microphone audible at all, and is
  also what feeds the mic back into the headphones. It stays **off**, and the
  gain is applied in software instead.
- The raw headset mic is very weak (~0.001 RMS, roughly nine bits of resolution),
  so software gain must be large. A naive AGC amplified the room itself to speech
  level and the VAD then fired on nothing. Hence the noise gate: audio below
  three times the measured floor passes through untouched.
- Capture always goes through pipewire. The headset hardware only runs at 48 kHz,
  so asking PortAudio for the raw device at 16 kHz fails outright.

## Bugs found and fixed (worth remembering, they were all subtle)
- **PortAudio frame aliasing.** `MicCapture` handed out views into the buffer the
  audio thread reuses, so every queued frame silently became the newest block. A
  20 s recording came back as a constant tone. Frames are copies now.
- **Whisper hallucinating into silence.** Filtered before it can be spoken aloud.
- **Runaway on room noise.** Continuous sound made the wake path record up to
  25 s and transcribe it, and `asyncio.to_thread` ran several transcriptions at
  once against a six-thread model: 271% CPU for minutes, silently. Transcription
  is serialised and the wake path caps recordings at 10 s.
- **Same mistake twice, both caught by tests:** a state-machine branch that reset
  the state but not the recording buffers, so it re-fired once per queued frame
  (1330 log lines in one second, then 1667 more). The pump now resets buffers on
  any state change and explicitly in the drop path.
- **pi's extensions** cost a few hundred ms per turn (donsetch spawns an MCP
  binary and handshakes at session start). Off for the voice path.

## Testing
`./tests/run_all.sh` runs 12 groups, all green, none needing a microphone:
stt, tts, wake model, wake phrase, the pi bridge, the pump state machine,
the software AGC, frame ownership, weak-mic accuracy, session memory, the
durable store, and transcription serialisation.

Style worth keeping: tests measure the real object where possible, deliberately
degrade audio to this microphone's actual level rather than testing on clean
audio, and compare by behaviour (memory ownership, concurrency) rather than by
value, because the AGC drives different inputs to the same level on purpose.

## Operating it
    ./run.sh                 voice loop + dashboard window
    ./run.sh --no-hud        console only
    ./run.sh --say "hi"      one typed message, no mic
    ./run.sh --mic-test      live mic check: level, speech score, what it heard
    ./tests/run_all.sh       everything that needs no microphone

Restarting (the kill pattern matters, a plain pattern also matches the shell
running the command):

    pkill -f '[j]arvis'
    cd ~/jarvis && setsid nohup uv run python -u -m jarvis --open \
        > /tmp/jarvis.log 2>&1 < /dev/null &

Log lines carry timestamps, the tool command it ran, and how long each answer
took to start speaking. `py-spy dump --pid <pid>` needs sudo and is the way to
find a busy loop. Note `ps` %CPU is a lifetime average, so measure with
`/proc/<pid>/stat` deltas before concluding anything is spinning.

## Known limits
- The headset microphone is the weakest link. Its raw output is very quiet, so a
  lot of software gain rides on a thin signal. A better microphone would improve
  recognition more than any further tuning.
- No pretrained wake model for this name, so the phrase is understood rather than
  matched: it will also accept just "maki", and it transcribes room speech
  locally while looking for it (nothing leaves the machine).
- Long-run stability is unproven. Sessions so far are minutes, not hours.
- Answers depend on the free quota through pi. When it runs dry, the local speech
  parts keep working and only the thinking suffers.
- The dashboard is unauthenticated and binds to loopback only.

## Next
- The email and security-sweep commands that were wanted at the start. Email needs
  an app password; the sweep needs rkhunter/lynis/clamav installed.
- If the wake phrase should require the full "hey maki" rather than "maki", that
  is a one-line change in `SpeechWake.match`.
