# Features

The full list, including the limits. Kept to one line each.

## Talking to it

- **Any wake phrase.** Recognised, not matched against a trained model, so you set
  your own words and no training is involved.
- **One breath.** "hey jarvis, what is the weather" is one turn, not two.
- **Interruption.** Talk over it and it stops mid sentence, keeps what you said,
  and answers that instead. The wake phrase again does the same.
- **It gives up on a slow model** if you start talking, not after.
- **Typed input.** A text box, for when a microphone is wrong.
- **A microphone switch.** Key `M` or the button. Off means off, across a restart.
- **It says when it cannot hear.** Twenty seconds of exact digital silence and it
  says so, once.

## Answered locally, without the model

Never touch a relay, so these land in about half a second:

- **Reminders and timers.** "Remind me in twenty minutes to check the oven", "set
  a timer for an hour and a half", "what reminders do I have". Parsed on the
  machine, confirmed in one breath.
- **A way back from clearing them.** Cancelling keeps what it took for two
  minutes: "undo" brings them back, with their times.
- **Themes.** Three, switched by voice.
- **Briefing facts.** Time, date, weather, load, memory, uptime and the last sweep,
  gathered locally and handed to the model, so it reports the machine.

## It knows the machine

- **Screen vision.** It captures the screen and hands the picture to the model, so
  the answer is about the window that is open rather than what a model remembers of
  the internet.
- **Security sweep.** Processes, listening sockets, SSH config, hosts file, world
  writable paths, user services, browser extensions, launcher entries, pending
  updates, and a bounded ClamAV scan of the usual drop points. It reports what is
  normal as well as what is not.
- **Memory.** Durable facts in a plain file you can edit by hand, given to the
  model every turn. Small on purpose.
- **A permission gate, off by default.** Switched on, every tool call is judged in
  code before it runs, not by a prompt sentence a bad page can argue with: its own
  directories and commands that only look are allowed, everything else waits for
  your approval, once, for that exact action.

## The dashboard

- **Live state.** What it heard, is doing, said, and every tool it used, streamed
  as it happens; the server is the source of truth, so two tabs agree.
- **Three themes.** Standard, stealth and combat, repainting the orb, the rings,
  the radar and every panel. Also `?theme=combat` on the URL.
- **It tells you where the turn is.** Thinking, listening, speaking and muted are
  distinct states, not one blinking light.

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
| "remember that the router is in the hall" | a durable fact, written to the memory file |
| anything else | the model, with shell and file tools |

Interrupt any of it by talking over it, or by saying the wake phrase again.

## Limits worth knowing

- **Reminders take a delay, not a clock time.** "Tomorrow at nine" gets a refusal
  with the shape it does understand, because a confident yes that never fires is
  worse than a plain no.
- **It works with no internet.** A small local model answers instead of a relay,
  measured here at 0.7 s cold; llama.cpp and a model fetched by hand are needed.
- **The fallback voice is an extra.** Without kokoro's files it says there is no
  usable speech engine rather than speaking with something worse;
  `uv sync --extra fallback-voice` adds piper.
- **Screen questions need a seeing model and a willing compositor.** A text only
  model answers without the picture, and some NVIDIA and Wayland pairs refuse a
  screenshot, which it retries three times.
- **Talking about it can wake it.** The wake phrase in a sentence about the
  assistant is a call as far as it is concerned.
- **On a scrolling layout the dashboard can open off the edge**, since a Wayland
  client cannot place its own window. `class = "brave-127.*"` matches it.
- **The sweep reads Linux and macOS, not Windows.** Asking there gets a plain
  answer instead of a sweep.
- **macOS gates two things.** Screen Recording for screen questions, Microphone for
  the microphone. There is also no temperature on Apple Silicon, so the dashboard
  shows nothing rather than a made-up number.
- **With the permission gate off, nothing is checked.** It ships off, and `python
  -m jarvis.permissions status` says so plainly instead of implying enforcement.
- **Email, calendar and music are not here.** Each needs an account and a
  credential.
