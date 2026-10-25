# Features

The full list, including the limits. Kept to one line each.

## Talking to it

- **Any wake phrase.** Recognised, not matched against a trained model, so you set
  your own words and no training is involved.
- **One breath.** "hey jarvis, what is the weather" is one turn, not two.
- **Interruption.** Talk over it and it stops mid sentence, keeps what you said,
  and answers that instead. The wake phrase again does the same.
- **It gives up on a slow model** if you start talking, rather than talking over
  you later.
- **Typed input.** A text box, for when a microphone is the wrong tool.
- **A microphone switch.** Key `M` or the button. Off means off, across a restart.
- **It says when it cannot hear.** A muted or disconnected microphone delivers
  exact digital silence, and after twenty seconds of it the assistant says so once.

## Answered locally, without the model

Never touch a relay, so these land in about half a second:

- **Reminders and timers.** "Remind me in twenty minutes to check the oven", "set
  a timer for an hour and a half", "what reminders do I have". Parsed on the
  machine, confirmed in one breath.
- **A way back from clearing them.** Cancelling says what it took and keeps it for
  two minutes: "undo" brings them back with the times they had.
- **Themes.** Three, switched by voice.
- **Briefing facts.** Time, date, weather, load, memory, uptime and the last sweep,
  gathered locally and handed to the model as facts, so it reports the machine
  rather than remembering it.

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

## The dashboard

- **Live state.** What it heard, what it is doing, what it said, every tool it
  used, streamed as it happens. The server is the source of truth, so two tabs
  agree.
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
| "undo", "bring them back" | whatever the last cancel took, if it was in the last two minutes |
| "remember that the router is in the hall" | a durable fact, written to the memory file |
| anything else | the model, with shell and file tools |

Interrupt any of it by talking over it, or by saying the wake phrase again.

## Limits worth knowing

- **Reminders take a delay, not a clock time.** "Tomorrow at nine" gets a refusal
  with the shape it does understand, because a confident yes that never fires is
  worse than a plain no.
- **The model is remote and free.** The relays stall; when one does the next takes
  over and it says so. A turn can end with "my connection to the model dropped".
- **The fallback voice is an extra.** With kokoro's model files missing it says
  there is no usable speech engine rather than speaking with something worse.
  `uv sync --extra fallback-voice` adds piper for that case.
- **Screen questions need a model that can see, and a compositor that will copy.**
  With a text only model the answer arrives without the picture, and some NVIDIA
  and Wayland combinations refuse a screenshot, which it retries three times.
- **Talking about it can wake it.** The wake phrase in a sentence about the
  assistant is a call as far as it is concerned.
- **On a scrolling layout the dashboard can open off the edge**, because a Wayland
  client cannot place its own window. `class = "brave-127.*"` matches it and a
  float rule fixes the placement.
- **The sweep reads Linux and macOS, not Windows.** Asking for one there gets a
  plain answer instead of a sweep. Everything else runs on all three.
- **macOS gates two things.** Screen Recording for screen questions, Microphone for
  the microphone, both under System Settings. Before the tick they read as a black
  frame and a microphone that opens onto nothing, and both are reported as what
  they are. There is also no temperature on Apple Silicon, since no unprivileged
  sensor exists; the dashboard shows nothing rather than a made-up number.
- **Email, calendar and music are not here.** Each needs an account and a
  credential, so they are left to whoever wants them.
