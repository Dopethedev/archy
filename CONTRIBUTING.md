# Contributing

A personal assistant that runs on one machine, so the useful contributions are the
ones that make it work better there: a local intent, a sweep check, a fix to
something that mishears, another platform's backend.

## Running the tests

```
./tests/run_all.sh
```

One group per behaviour, each runnable on its own (`uv run python
tests/test_reminders.py`), and none needing a microphone, the voice models or a
compositor: a suite that needs hardware is a suite nobody runs. The groups that
need neither hardware nor a model run on Linux, macOS and Windows in CI, on every
push.

Two groups need **node** 22 or newer, because it runs the files that really run
rather than a copy: the permission gate group executes the pi extension, and the
dashboard group executes the page's own script.

`./run.sh --mic-test` watches the live input level while you talk, and `uv run
python -m jarvis.fetch_models --check` says whether the models are where they
should be.

## The rules this repository follows

- **A test that cannot fail does not count.** Break the thing it checks and watch
  it fail. If it passes either way it is decoration.
- **Numbers come from runs, not from reading.** If a change is faster or quieter,
  the message says what was measured and how, including anything that argues
  against it.
- **Failure paths first.** A missing tool, a silent microphone, a stalled model, a
  truncated download.
- **The failure that matters is the quiet one.** Being understood and then silently
  dropped is worse than a refusal. If something cannot be done it says so, and says
  what it can do instead.
- **Local intents stay local.** Anything parseable on the machine must not go to a
  model. Add to `reminders.py` or the theme path in `assistant.py` rather than
  teaching the model a new trick.
- **No personal settings in tracked files.** Name, city, wake phrase, microphone,
  vocabulary and passwords live in `config.local.json`. Test fixtures bring their
  own values.
- **No em-dashes in prose.** Comments, commit messages, the README. They read as
  machine-written.
- **Say what you did not verify.** "The microphone on Windows is untested" is more
  useful than a claim that turns out to be false.

## Adding a local intent

Three parts, in `src/jarvis/reminders.py`, before the model is involved:

1. `wanted(text)` decides whether the request is this kind of request.
2. `parse(text)` returns an action or `None`. If it returns `None` while `wanted`
   said yes, add the honest refusal next to it (see `cannot_do`) rather than
   letting the model accept something it cannot do.
3. `confirm(action)` is what gets spoken, in one breath.

Then wire it into `Assistant._process` above the model call, and add a group to
`tests/`.

## Adding a sweep check

A function returning lines that each start with `ok:`, `warn:` or `alert:`. It
must report when it is clean, and say what it skipped when a tool is missing. The
failure mode to avoid is crying wolf: a check that flags normal noise trains its
reader to ignore the whole report. Add it to `CHECKS` and test it with the shapes
you expect, not just the happy one.

## Commits

Plain sentences, first person, and keep the reason. If you tried something and it
did not work, say so. That half of the story is what stops the next person trying
it again.
