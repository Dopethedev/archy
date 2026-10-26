# Features

The full list, including the limits. Kept to one line each.

## Talking to it

- **Any wake phrase.** Recognised, not matched against a trained model, so you set
  your own words and no training is involved.
- **One breath.** "hey jarvis, what is the weather" is one turn.
- **Interruption.** Talk over it and it stops, keeps what you said, and answers that.
- **It gives up on a slow model** if you start talking, not after.
- **Typed input.**
- **A microphone switch.** Key `M` or the button. Off means off, across a restart.
- **It says when it cannot hear.** Twenty seconds of digital silence, said once.

## Answered locally, without the model

Never touch a relay, so these land in about half a second:

- **Reminders and timers.** A delay, a clock time, or every day at a time, parsed on
  the machine and confirmed in one breath, including "what reminders do I have".
- **A way back from clearing them.** Cancelling keeps what it took for two minutes:
  "undo" brings them back, with their times.
- **Briefing facts.** Time, date, weather, load, memory, uptime, gathered locally.

## It knows the machine

- **Screen vision.** It captures the screen and hands the picture over, so the answer
  is about the window that is open.
- **Security sweep.** Processes, listening sockets, SSH config, hosts file, world
  writable paths, user services, browser extensions, launcher entries, pending
  updates, and a bounded ClamAV scan. It reports what is normal as well as what is
  not.
- **Memory.** Durable facts in a plain file you can edit by hand, given to the model
  every turn.
- **Windows.** What is open, and bringing one to the front by application name, both
  answered without the model. Closing only ever closes the window in front.
- **A permission gate, off by default.** Switched on, every tool call is judged in
  code before it runs: its own directories and commands that only look are allowed,
  everything else waits for your approval, once.
- **Privacy switches for online models.** What it remembers, the files you ask
  about, screenshots, earlier conversation and transcripts, each one switchable,
  each enforced where the request is built rather than asked for in a prompt. All
  on by default, which is what it already did, and the local model is never
  restricted because nothing leaves the machine.

## The dashboard

- **Live state.** What it heard, is doing, said, and every tool it used, streamed
  as it happens; two tabs agree.
- **Three themes.** Standard, stealth and combat, repainting the orb, the radar and
  every panel. Also `?theme=combat` on the URL.
- **It tells you where the turn is.** Thinking, listening, speaking and muted are
  distinct states, not one blinking light.
- **What the assistant itself is doing.** Which model is answering and whether it is
  this machine's, the network verdict, the local server, what may leave the machine,
  and what waits on your approval. Off by default, so the panel appears when asked.

## What you can say

Against the actual matchers, so these all work as written:

| Say this | What happens |
|---|---|
| "combat mode", "go to stealth mode", "back to normal", "red alert" | the dashboard changes theme |
| "brief me", "good morning", "what's the weather", "how's the machine" | a spoken briefing, facts gathered locally |
| "run a security sweep", "am I infected", "virus scan", "check my computer" | the sweep runs and is summarised |
| "what's on my screen", "look at this error", "what am I looking at" | a screenshot is taken and read |
| "remind me in twenty minutes to check the oven" | a reminder, confirmed at once, spoken when due |
| "set a timer for five minutes", "set a timer for an hour and a half" | a timer with an alarm |
| "what reminders do I have", "cancel my reminders" | the list, or all cleared, with two minutes to undo |
| "undo", "bring them back" | whatever the last cancel took |
| "remember that the router is in the hall" | a durable fact, in the memory file |
| anything else | the model, with shell and file tools |

Interrupt any of it by talking over it, or by saying the wake phrase again.

## Limits worth knowing

- **Date shapes are still refused.** "Next week", "on the 14th" and "in two weeks"
  get one plain sentence, because a confident yes that never fires is worse than a no.
- **It works with no internet.** A small local model answers instead of a relay,
  measured here at 0.7 s cold; llama.cpp and a model fetched by hand are needed.
- **The fallback voice is an extra.** `uv sync --extra fallback-voice` adds piper;
  without kokoro's files it says so rather than speaking with something worse.
- **Screen questions need a seeing model and a willing compositor.** A text only
  model answers without the picture, and some NVIDIA and Wayland pairs refuse the
  screenshot.
- **Talking about it can wake it.** The wake phrase in a sentence about the
  assistant is a call as far as it is concerned.
- **On a scrolling layout the dashboard can open off the edge**, since a Wayland
  client cannot place its own window.
- **The sweep reads Linux and macOS, not Windows.** Asking there gets a plain
  answer instead of a sweep.
- **macOS gates two things.** Screen Recording and Microphone. There is also no
  temperature on Apple Silicon, so the dashboard shows nothing rather than a number.
- **With the gate off, nothing is checked**, and `python -m jarvis.permissions status` says so.
- **Email, calendar and music are not here.** Each needs an account and a
  credential.
