# status

## State
Working and in use. A turn takes about **1.4 s** from the end of speech to the
first spoken word, and start-up is a second or two. Verified live: wake phrase,
transcription, a real shell command, a spoken answer, and the dashboard.

Run it with `./run.sh`. Wake phrase is **"hey maki"** (just "maki" also works).

Where a turn's time goes, measured on this machine:

    1. transcribe what was said            0.40 s
    2. the model's first words             0.47 s
    3. speaking the opening clause         0.52 s
                                           ------
                                           1.39 s

The third used to be 2.77 s, because the voice waited for a whole sentence
before saying anything. Kokoro synthesises at roughly real time, so that was
2.77 s of silence. It starts on the first clause now (`split_ready(first=True)`),
which was worth more than everything else in this file put together.

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
- **Focused listening.** While waiting for a request the microphone listens
  harder: `AGC_TARGET_FOCUSED = 0.20` against 0.12, `AGC_GATE_FACTOR_FOCUSED = 2.0`
  against 3.0, `SPEECH_ON_FOCUSED = 0.22` against 0.35. Measured: soft speech
  comes through 66% louder, room noise stays 250x below speech, nothing clips.
  It is enabled by `set_state("listening")` and only there: leaving it on while
  it speaks would feed the barge detector a hotter signal. Idle does not need it,
  because a missed wake phrase only costs saying it again, while the seconds
  after the beep are the one place a quiet word is lost for good.
- `PI_CONTINUE = True` (memory survives restarts), `PI_EXTENSIONS = False`,
  `PI_THINKING = "off"`, `BRAIN_SILENCE_TIMEOUT_S = 45` (a deadline on silence,
  not on total time, so a long tool run is not cut off but a hung one is).
- Barge-in: 150 ms of speech cuts off a reply (`BARGE_FRAMES = 5`), 400 ms is
  needed to cut off the thinking (`BARGE_FRAMES_THINKING = 13`), because nothing
  is audible while it thinks and a stray word would silently eat the answer.
  `BARGE_GRACE_MS = 500`, and the audio that triggered it is kept so the first
  word is not swallowed.
- The voice cuts its first chunk on a clause (`FIRST_CHUNK_MIN = 12`,
  `FIRST_CHUNK_MAX = 48`) and every later chunk on a sentence. Nothing is lost or
  duplicated doing it: a test joins the chunks and compares against the original.
- The dashboard mints a token per run (`hud.token`) and refuses a request
  without it, from another origin, or under a foreign Host header. This is a
  control surface for something that runs commands, so loopback alone is not
  enough: any page in the browser can POST to a local port without a preflight.

## The dashboard
A page at `127.0.0.1:8731`, standard library only, fed by server-sent events. An
orb whose colour follows the state, a live caption of what it heard and what it
replied, machine stats, a reply-delay readout, and an activity log. Typing in the
box is handled exactly like speech.

Two things there are easy to get wrong, and were:
- Levels were published every 10 ms, which is a hundred events a second of JSON:
  five times more than the eye can use, and enough to wake the browser
  constantly. Now 20 a second (`LEVEL_MIN_GAP`). Levels are also never put in the
  replay buffer, so a tab opened later is told the current state and the recent
  transcript instead of the last half second of microphone level.
- The caption is fed by `_publish_heard`. The speculative transcript goes up as
  soon as it exists, so words appear while you are still finishing the sentence;
  waiting for the turn to complete made the screen look dead.

On drawing: `hud/app.js` renders the orb as a dark sphere with a bright rim and
one highlight, not a soft radial glow, because a glow reads as a blur. A
screenshot of the running dashboard is the only honest way to judge it.

### The microphone toggle
Header button, or the `M` key (which it ignores while you are typing). The button
posts `/mic` and then follows the state that comes back over the event stream
rather than flipping a local flag, so whoever pressed it, the page shows what is
actually true.

Off closes the capture stream; on reopens it. Muting is a state of its own (red,
"microphone off"), so a dead microphone never looks like a live one, and
`_back_to_idle` returns to `muted` instead of `idle` while it is off. In the
`muted` state audio does nothing at all: no wake, no request, no interrupt, and
a test sends loud speech through the pump to keep it that way. Typed input still
works, which is most of the reason to have the switch. Not persisted: a restart
starts live, and a microphone that fails to reopen is reported instead of being
assumed on.

`/mic` sits behind the same token as `/say`. The microphone is the other way
into the assistant, so a page that could switch it on could listen to the room.

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
- **Interrupting left a fragment of the old reply to play.** A reply is spoken
  sentence by sentence and synthesis runs at about real time, so at the moment
  the user cuts in the speaker is almost always mid-sentence. That chunk was
  queued regardless of the flush, so seconds later a piece of the answer they had
  just stopped arrived and played over the new one. Reported as "it lags and
  glitches for a few seconds". `Player` now carries a generation: `flush()`
  invalidates, `start_reply()` claims a new one, and a write from an older reply
  is refused under the same lock. Audio that is refused does not count as spoken
  either, so the state machine cannot report "speaking" over silence.
- **A single uncancellable synthesis could be 3.1s of wasted work.** onnxruntime
  cannot be interrupted mid-run, so whatever is being synthesised when the user
  cuts in keeps burning CPU while the new reply waits its turn. A sentence is now
  capped at `SENTENCE_MAX = 90` characters, cut at a clause or a space: measured
  worst case falls from 3.13s to 1.22s.
- **The opening chunk took the first comma wherever it fell.** A sentence that
  only punctuates near the end was held back whole, so one late comma cost
  seconds before the first word was heard, the exact thing the early cut exists
  to prevent. The clause now has to fall inside `FIRST_CHUNK_MIN..FIRST_CHUNK_MAX`.
- **A hung model process hung the assistant.** Nothing had a deadline, so a
  silent `pi` left it stuck in "thinking" with no way back but a restart.
- **The give-up deadline was measured from a stale timestamp.** A task arms
  listening when the beep plays; if no audio frame was queued at that instant the
  wait was counted from the last arm, so it gave up immediately: the log showed
  the wake phrase and "nothing said, going quiet" in the same second. Reported
  as "it beeps and then stops after a second". The state is synced at the top of
  every pump iteration now, not only when a frame arrives.
- **A blip ended the turn.** The tail of the wake phrase, or a cough, was enough
  to send it back to idle, so a request starting a moment later was never heard.
  In the listening state a too-short utterance now clears and keeps waiting, with
  the deadline still running.
- **Fragile test setup, three times over.** Tests build `MicCapture` with
  `__new__` to exercise the gain stage alone, and each new field broke another
  one of them. The audio state is declared at class level as well as in
  `__init__` now, so a default that already exists cannot be forgotten.
- **The same deadline trap, one state over.** The listening give-up timeout was
  only checked when a frame arrived, so a stalled microphone would wait forever.
  It also made the test covering it flaky rather than wrong, which is how it was
  found: it passed twice and failed on the third run.
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

### Memory and disk, after the pass that removed two imports
| | before | after |
|---|---|---|
| import chain to a working VAD | 186 MB, 0.75 s | **85 MB, 0.10 s** |
| scipy, imported for one call | 107 MB, 660 ms | replaced by soxr (28 MB, and that is numpy) |
| openWakeWord, imported for its VAD | 159 MB, 740 ms | replaced by `vad.py` on onnxruntime (10 MB) |
| whole live process | 1064 MB | 1022 MB |

The live figure barely moves because two thirds of it is the models themselves:
860 MB of the 1022 MB is anonymous heap, which is kokoro's weights, whisper's
weights and their buffers. The import savings show up in start-up, in every
process that does not need the models, and in what a fresh process costs.

Measured and deliberately not done:
- **Whisper is already int8** (156 MB less than float32) and the rest is
  ctranslate2's own overhead, so there is nothing left to win without a smaller
  model, which would cost accuracy on this microphone of all things.
- **fp16 kokoro** saves 159 MB but does not reproduce the fp32 audio. The
  release claims 0.999 spectral correlation; measured 0.70, with a 1.9 percent
difference in length, and the file is a different export version as well as a
different precision. Both renders are in `voice-samples/kokoro_fp32.wav` and
  `kokoro_fp16.wav` to be judged by ear, because that is the only honest test.
- **av** costs 22 MB and cannot be dropped cleanly: faster-whisper declares it,
  even though the only thing that uses it is file decoding we never call.
- **openWakeWord on disk** is 170 MB with scikit-learn and scipy behind it, all
  of it for `WAKE_MODE = "model"`, which is not the mode in use. Kept because the
  mode is supported and its tests cover it.

Disk: the project is 1.3 GB, of which `.venv` 672 MB, models 562 MB (including
the 164 MB fp16 sample), the rest a few MB. The huggingface cache is 12 GB, and
this project needs **139 MB** of it: whisper's base.en. The rest is from earlier
experiments (a 4.3 GB Qwen TTS among them) and is not referenced by anything
here.

## The bug hunt (what a full read of the code found)
A pass over every file found and fixed nine, several of them the kind that
only show up on a bad day:

- The error paths spoke with `speaker.say` directly on the event loop. Synthesis
  runs at about real time, so a failed turn stalled the pump for seconds, the
  microphone queue backed up, and on resume the pump replayed stale audio as if
  fresh: the tail of his own voice could return as a phantom second request.
  Both handlers and the start-up greeting now synthesise off the loop.
- Turn tasks were spawned with `create_task` and the reference dropped. The
  event loop holds tasks only weakly, so a turn could in principle be collected
  mid-flight. Every task is pinned in a set now, and `_spawn` creates its set
  lazily so the `__new__`-built test objects keep working.
- Tests shared the live session directory. Every brain test appended its own
  chatter (search "purple lantern") into his real conversation, which had grown
  to 596 KB and made `--continue` slow enough to trip the deadline.
  `JARVIS_SESSION_DIR` now isolates them, and the three brain tests use it.
- `.state/` (sweep reports, reminders, personal text) was committed to the
  repository. Removed from the index and ignored.
- "What time is it" paid a weather fetch on a cold cache: 9 s for a 1.4 s
  question. Time-and-date-only asks now gather local facts only.
- A failed synthesis silently swallowed a due reminder: it was removed from the
  store, then never spoken. `Speaker.say` now reports whether anything was
  spoken, and an undelivered reminder returns to the store for a 60 s retry.
- The dashboard header stayed on "reconnecting" forever after a brief SSE
  drop, because the replayed state event was swallowed by an unchanged-value
  early return. The label updates on every state event now.
- Theme matching matched the bigram anywhere: "what is the best combat mode in
  this game" repainted the screen. Only command shapes count now: the name
  leading the sentence behind a small set of lead-ins, the theme-to-name form,
  and reset/restore. Verified live against the game question.
- The briefing read the whole sweep header aloud ("the last sweep, security
  sweep of Archy, 12:50:"). It reads the file's own timestamp instead.
Dead config removed (PI_MODEL had no readers left), two unused imports dropped.

## The six upgrades (chain, briefing, reminders, themes, boot film, radar)
The brain is a chain now, not a pin: wandb GLM-5.3-Flash, the same model on
crusoe, Kimi-K2.6, Qwen3.8-27B, every one verified answering through pi with
images. A model that errors (404, upstream dead, process ended) moves the
assistant to the next entry and says so; a silence hang only restarts, because
a hang is not proof the model died. This is the fix for the silent morning.

Local intents answered without any model: reminders and timers ("remind me in
20 minutes to check the oven", "set a timer for 5 minutes", "cancel my
reminders", "what reminders do I have"), confirmed in 0.6 s, stored in
.state/reminders.json so a restart does not eat them, and spoken by a
background loop when due, two blips then the reminder, waiting for a quiet
moment first. And dashboard themes: "stealth mode", "combat mode", "standard
mode" (the name must ride with mode or theme, so a mention of combat in a
sentence does not repaint the screen). Three palettes, verified in
screenshots, broadcast to every open tab with a re-tune glitch on landing.

The briefing: "brief me", "good morning", "what's the weather" gathers time,
date, open-meteo weather for Kathmandu (no key, cached half an hour, honest
"unavailable" on a dead network), machine load and memory, and the last
sweep's verdict from .state/last-sweep.txt, and hands the facts to the model
with instructions not to search the web for them.

The dashboard grew the film: a typed kernel-log boot sequence (skippable with
#noboot for screenshots), a waveform of the last second and a half wrapped
around the orb as radial spokes, cpu and memory drawn as telemetry arcs
around it, and a proximity radar panel whose sweep blips on real events:
what was heard, replies, commands, state changes. Verified live: boot film,
all three themes, a timer that fired and spoke 11 s after being set, a
briefing with the real time in it. 24 test groups.

## The security sweep, extended to the browser and the launchers
Two check families were added, both verified against planted copies and against
the live machine.

The browser check reads each Brave profile's Preferences, classifies every
loaded extension (store, unpacked, component), demands manifest-level
justification for dangerous permissions (all_urls, tabs, webRequest, cookies,
history, debugger, nativeMessaging, and friends, each with a spoken reason),
warns on installs younger than two days, and alerts on a component whose
payload no package owns. Brave's component payload lives in root-level
<id>/<version> dirs under the browser folder, and that is where ownership is
verified, one batched pacman call: the Preferences paths for bundled components
are often resource dirs with no manifest of their own. The binary and any flash
plugin are verified with pacman -Qkk against the checksums recorded at install
time. The live machine: 4 extensions accounted for, binary clean, no flash.

The launcher check walks /usr/share/applications, /etc/xdg/autostart,
~/.local/share/applications, ~/.config/autostart and ~/.config/systemd/user,
resolves every Exec/ExecStart through field codes, env wrappers and PATH, and
judges each target three ways: scratch directory (alert), no package owner
(alert in /usr, recency-based elsewhere), and newer than the last pacman
transaction without an owner (warn). Unowned targets go to clamav as well,
capped at 40. /usr/local/bin is judged by age, not alerted: it is where local
admin scripts live by design, and davinci-convert lives there.

Method: every bad condition is simulated on a copy in tests/sim (never /tmp,
because the scratch-path rule would fire on the test's own fixtures, which it
did until the ground moved), never on the live system. 22 test groups.

## The security sweep
"run a security sweep", "am i infected", "virus scan" and friends trigger a
local sweep (sweep.py), whose report is handed to the model to summarise for
speech. Nine checks: processes running from /tmp, /dev/shm or deleted files
(upgraded system binaries warn, they do not alert), uid 0 accounts, /etc/hosts
hijacks, world-writable executables, listeners beyond loopback and the desktop's
own discovery services, ssh keys, user systemd units launching from scratch
directories, pending updates, and clamav over /tmp, /dev/shm and ~/Downloads
with a 120 s cap. Every check reports even when clean: silence is how a skipped
check hides. Two tuning rules learned the hard way: flagging mDNS and resolver
stubs trains the reader to ignore the report, and daily clamav updates arrive
as .cld, not .cvd. The sweep says what it does not cover: the browser, running
app-level malware, everything outside the scanned folders. 21 test groups.

## Screen vision
Ask about the screen and it looks: "what's on my screen", "look at this",
"what is this error". The trigger list is `SCREEN_WORDS` in config, deliberately
eager, because a missed look means a blind answer and an unneeded capture only
costs a screenshot. grim pipes a full-resolution frame through ffmpeg to a
quality-85 JPEG (about 85 kB) and the turn carries it to the model as an image;
nothing touches disk. Resolution matters: at 1280 px the model misread the
dashboard's small text, at full size it reads it. The pinned GLM-5.3-Flash sees
(2.8 s probe on the raw PNG, 4.2 s on the jpeg), a failed capture falls back to
answering blind and says so. 20 groups, the new one being test_vision.

## Testing
`./tests/run_all.sh` runs 20 groups, all green, none needing a microphone:
stt, tts, tts streaming latency, tts interruption, wake model, wake phrase, the
pi bridge, the brain deadline, the pump state machine, the software AGC, the
listening focus, the microphone toggle, frame ownership, weak-mic accuracy,
session memory, the durable store, transcription serialisation, the VAD, and the
dashboard's security.

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
