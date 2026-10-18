# Contributing

This is a personal assistant that runs on one machine, so the useful
contributions are the ones that make it work better there: a local intent, a
sweep check, a fix to something that mishears, another platform's backend. What
follows is how the repository is actually written, which is a better guide than
any style guide I could invent.

## Running the tests

```
./tests/run_all.sh
```

One group per behaviour, each runnable on its own (`uv run python
tests/test_reminders.py`), and none of them needing a microphone, the voice
models or a compositor. That is deliberate: a suite that needs hardware is a
suite nobody runs. The microphone-free groups also run on Windows in CI, on
every push.

Two commands worth knowing: `./run.sh --mic-test` watches the live input level
while you talk, and `uv run python -m jarvis.fetch_models --check` says whether
the models are where they should be.

## The rules this repository follows

- **A test that cannot fail does not count.** Before you trust a check, break the
  thing it is checking and watch it fail. If it passes either way it is
  decoration, and there are commit messages in here that say so about checks I
  thought were fine.
- **Numbers come from runs, not from reading.** If a commit message says a change
  made something faster or quieter, it says what was measured and how, including
  the measurement that argues against the change.
- **Failure paths first.** A missing tool, a silent microphone, a stalled model, a
  truncated download. The interesting question is almost never what happens when
  things work.
- **The failure that matters is the quiet one.** A request that is understood and
  then silently dropped is worse than one that is refused. If something cannot be
  done, it says so and says what it can do instead.
- **Local intents stay local.** Anything that can be parsed on the machine must
  not go to a model: reminders, timers and themes are answered in about half a
  second and never touch a relay. Add to `reminders.py` or the theme path in
  `assistant.py` rather than teaching the model a new trick.
- **No personal settings in tracked files.** Name, city, wake phrase, microphone,
  vocabulary and any password live in `config.local.json`, which is ignored.
  Fixtures in tests bring their own values instead of inheriting yours, and the
  Windows job would notice if a module imported something a platform lacks.
- **No em-dashes in prose.** Comments, commit messages, the README. They read as
  machine-written, which is a shame in a repository whose whole voice is that it
  was checked rather than generated.
- **Say what you did not verify.** "The microphone on Windows is untested" is a
  more useful sentence than a claim that turns out to be false.

## Adding a local intent

The pattern, in three parts, all in `src/jarvis/reminders.py` before the model is
ever involved:

1. `wanted(text)` decides whether the request is this kind of request.
2. `parse(text)` turns the sentence into an action, or returns `None`. If it
   returns `None` while `wanted` said yes, add the honest refusal next to it (see
   `cannot_do`) rather than letting the model accept something it cannot do.
3. `confirm(action)` is what gets spoken, in one breath.

Then wire it into `Assistant._process` where the other local intents are, above
the model call, and add a group to `tests/`.

## Adding a sweep check

A check is a function returning a list of lines, each starting with `ok:`,
`warn:` or `alert:`. It must report when it is clean, and it must say what it
skipped when a tool it needs is missing. `cry wolf` is the failure mode to avoid:
a check that flags normal noise trains its reader to ignore the whole report. Add
it to `CHECKS` and test it with the shapes you expect, not just the happy one.

## Commits and pull requests

Commit messages here say what changed and why it was wrong before, in plain
sentences, first person. Keep the reason. If you tried something and it did not
work, say so: that half of the story is what stops the next person trying it
again. A pull request from someone else is reviewed the same way, one test group
per behaviour and the failure path covered.
