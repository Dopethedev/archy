# Features

A voice assistant that runs on your own machine. Everything that hears and
speaks is local; only the thinking goes to a model. This is the full list,
including the parts that are limited, so nothing here surprises you after a week
of use.

## Talking to it

- **Any wake phrase.** The phrase is recognised, not matched against a trained
  model, so you set your own words and no training is involved. The dashboard
  shows the phrase you configured.
- **One breath.** The request can arrive in the same utterance as the wake word:
  "hey jarvis, what is the weather" is one turn, not two.
- **Interruption.** Talk over it and it stops mid sentence, keeps what you said,
  and answers the new request. Saying the wake phrase again does the same.
- **It gives up on a slow model.** Start talking while it is still waiting on a
  model and it drops that wait instead of talking over you later.
- **Typed input.** The dashboard has a text box, for when a microphone is the
  wrong tool. Press Enter to send.
- **A microphone switch.** Key `M` or the button. Off means off, and it stays
  off across a restart.
- **It tells you when it cannot hear.** A microphone that is muted or
  disconnected delivers exact digital silence, and after twenty seconds of it
  the assistant says so out loud, once.

## Answered locally, without the model

These never touch a relay, so they land in about half a second:

- **Reminders and timers.** "Remind me in twenty minutes to check the oven",
  "set a timer for an hour and a half", "what reminders do I have", "cancel my
  reminders". The wording is parsed on the machine and confirmed in one breath.
- **Themes.** Three dashboard themes, switched by voice, with no model in the
  loop.
- **Briefing facts.** Time, date, weather, load, memory, uptime and the last
  sweep are gathered locally and handed to the model as facts, so it reports the
  machine rather than remembering it.

## It knows the machine

- **Screen vision.** Ask about what is on screen and it captures the screen and
  hands the picture to the model. The answer is about the window that is
  actually open, not about what a model remembers of the internet.
- **Security sweep.** One request runs a set of checks: running processes,
  listening sockets, SSH configuration, the hosts file, world writable paths,
  user services, browser extensions, launcher entries, pending package updates,
  and a ClamAV scan of the usual drop points. It reports what is normal as well
  as what is not, and the scan is bounded so it cannot quietly eat the evening.
- **Briefing.** A spoken summary of time, date, weather, machine load, memory,
  uptime, and how the last sweep went.
- **Memory.** Durable facts in a plain file you can read and edit by hand, given
  to the model on every turn, so remembering costs no round trip. Ask it to
  remember something and it writes there; the store is small on purpose.

## The dashboard

- **Live state.** What it heard, what it is doing, what it said, and every tool
  it used, streamed as it happens. The server is the source of truth, so two
  open tabs agree.
- **Three themes.** Standard, stealth and combat, switched by voice, repainting
  the orb, the rings, the radar and every panel. A theme can also be set by
  opening the page with `?theme=combat`, which is what the screenshots use.
- **It tells you where the turn is.** Thinking, listening, speaking and muted are
  distinct states, not one blinking light.

## What you can say

Measured against the actual matchers, so these all work as written:

| Say this | What happens |
|---|---|
| "combat mode", "go to stealth mode", "back to normal", "red alert" | the dashboard changes theme |
| "brief me", "good morning", "what's the weather", "how's the machine" | a spoken briefing, facts gathered locally |
| "run a security sweep", "am I infected", "virus scan", "check my computer" | the sweep runs and is summarised |
| "what's on my screen", "look at this error", "what am I looking at" | a screenshot is taken and read |
| "remind me in twenty minutes to check the oven" | a reminder, confirmed at once, spoken when due |
| "set a timer for five minutes", "set a timer for an hour and a half" | a timer with an alarm |
| "what reminders do I have", "cancel my reminders" | the list, or all of them cleared |
| "remember that the router is in the hall" | a durable fact, written to the memory file |
| anything else | the model, with shell and file tools, so it can search, read and work things out |

Interrupt any of it by talking over it, or by saying the wake phrase again.

## Limits worth knowing

- **Reminders take a delay, not a clock time.** "In twenty minutes" and "in an
  hour and a half" work. "Tomorrow at nine" gets an honest refusal with the
  shape it does understand, because the model has no reminder tool and a
  confident "yes" that never fires is worse than a plain no.
- **The model is remote and free.** The chain is made of free relays, and they
  stall. When one does, the next takes over and it says so. Occasionally a turn
  ends with "my connection to the model dropped, ask me again".
- **Screen questions need a model that can see.** With a text only model the
  answer arrives without the picture.
- **Screen capture is not always reliable.** On some NVIDIA and Wayland
  combinations the compositor refuses a screenshot. It retries three times
  before giving up on that attempt.
- **Talking about it can wake it.** Saying the wake phrase while telling someone
  about the assistant is a call as far as it is concerned.
- **The sweep needs a Linux machine.** It reads `/proc`, asks systemd and the
  package manager, and looks in browser profiles. On Windows, or on a distribution
  without those, asking for one gets a plain answer instead of a sweep. Everything
  else runs on Linux and Windows both: the screen is captured through grim on
  Wayland and through Pillow elsewhere, and the launcher for Windows is `run.cmd`.
- **Email, calendar and music are not here.** Reading someone's mail means
  shipping their credentials and sending their messages to a model, so that one
  is left to whoever wants it. See the note in the README.
