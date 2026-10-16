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
- `WHISPER_MODEL = "base.en"`, beam 5, `WHISPER_TEMPERATURE = 0.0`, plus
  `WHISPER_HINT` listing his words. Measured 0.47 s for 4.3 s of audio against
  small.en's 1.36 s with identical accuracy: the hint is what fixed names like
  Sober and Kathmandu, not the larger model.
- `WHISPER_TEMPERATURE = 0.0` is not a detail. Whisper's temperature fallback
  retries a decode when the result looks bad, and on room noise every attempt
  looks bad, so it walks the whole chain at a full decode each time. Measured:
  noise 2.89 s against 0.31 s, real speech 4.12 s against 0.90 s, same output.
  Start-up went from 51 s to 7 s under load, because warming up transcribes
  noise, which was the worst case.
- `END_SILENCE_MS = 700`, `WAKE_IDLE_SILENCE_MS = 500`, `SPECULATE_AT_MS = 300`.
- Transcription is serialised by a lock. `WAKE_MAX_UTTERANCE_S = 10`,
  `MAX_UTTERANCE_S = 20`.
- `HARDWARE_AGC = False`, `SOFT_AGC = True` with a noise gate.
- `PI_CONTINUE = True` (memory survives restarts), `PI_EXTENSIONS = False`,
  `PI_THINKING = "off"`, `BRAIN_SILENCE_TIMEOUT_S = 45` (a deadline on silence,
  not on total time, so a long tool run is not cut off but a hung one is).
- Barge-in: 150 ms of speech cuts off a reply (`BARGE_FRAMES = 5`), 400 ms is
  needed to cut off the thinking (`BARGE_FRAMES_THINKING = 13`), because nothing
  is audible while it thinks and a stray word would silently eat the answer.
  `BARGE_GRACE_MS = 500`, and the audio that triggered it is kept so the first
  word is not swallowed.
- The dashboard mints a token per run (`hud.token`) and refuses a request
  without it, from another origin, or under a foreign Host header. This is a
  control surface for something that runs commands, so loopback alone is not
  enough: any page in the browser can POST to a local port without a preflight.

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
- **Temperature fallback on noise.** See the settings above. 4.5x the cost for
  the same words, on the single most frequent input the app ever sees.
- **The dashboard accepted orders from any website.** A page on any site can
  POST to a loopback port as a simple request, no preflight, and this assistant
  runs shell commands. Proven with a hand-written request, then fixed with a
  per-run token plus origin and host checks. The static handler also served
  files from any directory whose name merely started with `hud`, which a string
  prefix check allowed; it is `is_relative_to` now.
- **A hung model process hung the assistant.** Nothing had a deadline, so a
  silent `pi` left it stuck in "thinking" with no way back but a restart.
- **Typing while it was talking ran two turns at once:** two voices, two
  answers, one state neither of them owned.
- **Two writers to the durable store lost each other's change.** The assistant
  stores facts by running `memory_store` as a command, so a second process is
  normal. Writes are serialised with a file lock and re-read under it.
- **Same mistake twice, both caught by tests:** a state-machine branch that reset
  the state but not the recording buffers, so it re-fired once per queued frame
  (1330 log lines in one second, then 1667 more). The pump now resets buffers on
  any state change and explicitly in the drop path.
- **Start-up claimed to be speaking.** The greeting set the state to "speaking"
  and it stayed there through warm-up, so the dashboard lied for a minute.
- **pi's extensions** cost a few hundred ms per turn (donsetch spawns an MCP
  binary and handshakes at session start). Off for the voice path.

## Numbers, measured rather than assumed
- A turn: **1.4 s** from the end of speech to the first spoken word, when the
  machine is not otherwise busy. Reproduced live after the audit.
- Transcription: 0.47 s for 4.3 s of audio.
- The voice: kokoro runs at about **1.0x realtime**, meaning a reply takes about
  as long to generate as it lasts. It is delivered sentence by sentence, so the
  delay to the first word is only the first sentence. An earlier note in this
  file claimed 3x; that number was wrong and could not be reproduced.
- Start-up: 7 s under load, of which the recogniser warm-up is most.
- `ps` reports a process's CPU averaged over its whole life. Use `/proc/<pid>/stat`
  deltas instead, or multi-core spinning is easy to both invent and miss.

## Testing
`./tests/run_all.sh` runs 14 groups, all green, none needing a microphone:
stt, tts, wake model, wake phrase, the pi bridge, the brain deadline, the pump
state machine, the software AGC, frame ownership, weak-mic accuracy, session
memory, the durable store, transcription serialisation, and the dashboard's
security.

Style worth keeping: tests measure the real object where possible, deliberately
degrade audio to this microphone's actual level rather than testing on clean
audio, and compare by behaviour (memory ownership, concurrency) rather than by
value, because the AGC drives different inputs to the same level on purpose.
Two of them exist because the first version of the same test could not have
failed: six writer processes passed by luck, and the double-turn check had no
turn running to conflict with.

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
