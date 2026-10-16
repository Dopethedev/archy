# Soak / audit prompt

Paste this to a coding agent (pi, or any agent with shell access on this box) to
stress and audit the jarvis voice assistant. Replace the bracketed bits.

---

You are auditing and soak-testing `jarvis`, a local voice assistant living at
`/home/dope/jarvis`. Read `project.md` and `status.md` first, then test against
the real running system, not a synthetic stand-in.

## What to do

1. **Baseline the checks that need no microphone.**

       cd /home/dope/jarvis && ./tests/run_all.sh

   Read the output, do not trust the exit code alone. Confirm each test actually
   asserted something meaningful. If one "passes" in a way that would not fail
   against broken code, say so.

2. **Soak test.** Start the assistant with the voice loop running, or run the
   typed path in a tight loop if no microphone is available:

       ./run.sh --no-hud

   Then, over at least [30 minutes / 2 hours], sample and log:
   - process RSS and CPU for the pi process, the python assistant, and the
     whisper/kokoro workers (`ps -o pid,rss,%cpu,etime,cmd --ppid ...`)
   - file descriptor count (`ls /proc/<pid>/fd | wc -l`)
   - thread count
   - error/warning lines from the assistant log

   A steady rise in RSS or fd count is a leak. Report the slope, not a snapshot.

3. **Long-run correctness.** After the soak, confirm the assistant still
   transcribes, still answers, still interrupts, and its memory facts still load.
   A process that stays up but degrades is worse than one that crashes.

4. **Failure paths, not the happy path.** Adversarially test:
   - speak/type while it is mid-answer (interruption)
   - a very long utterance (large input)
   - rapid successive turns (turn-boundary races)
   - kill the `pi` brain process mid-turn and see if it recovers
   - corrupt `memory.json` and restart (must degrade to empty, not crash)
   - exhaust or throttle the Gemini quota and confirm the local speech parts
     keep working while only thinking fails

5. **Read the assertions.** For every test you claim passed, quote the line that
   asserts the behavior and explain why it would fail if the code broke.

## Rules

- Test against the live system: real microphone, real `pi`, real models. An
  isolated mock proves nothing about production.
- Never destructive-test the real data. Copy `memory.json` before you corrupt it.
- Report RSS/fd/thread slopes and the exact commands you ran.
- Do not declare anything "verified" or "stable" unless it survived the full soak
  and the failure-path set above. If it did not, say what broke and how.

## Output

A short report: what held, what leaked or degraded, what broke, and the single
highest-value fix. No filler.
