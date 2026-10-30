"""Writing a file in one step, under a name no other writer is holding.

Every file this is used for has two writers on purpose: the assistant and the
shell run as separate processes. A staging file with a fixed name is shared state
between them, and sharing it is how one writer installs a document another writer
is halfway through writing: what lands is truncated, and a truncated file reads as
an empty one rather than as an error. An empty `reminders.json` is no reminders,
and an empty `microphone.json` means the microphone is on.

The name therefore carries the writer's pid and thread id, so two writers cannot
be holding the same scratch file, and it is removed in a `finally` so an
interrupted write leaves nothing behind.

`memory_store.py` keeps its own copy of this because it also holds a
cross-process lock and retries the final move on Windows, where a file another
process has open cannot always be replaced on the first try.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path


def staging(path: Path | str) -> Path:
    """The scratch name one writer stages under, which no other writer shares."""
    path = Path(path)
    return path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def write(path: Path | str, body: str, mode: int | None = 0o600) -> Path:
    """Write the whole body, then put it in place, then tidy up.

    `mode` is applied after the move, and `None` leaves the file's own mode alone:
    a file that already exists keeps the permissions its owner gave it.

    The encoding is written out rather than left to the locale. It is the one place
    every state file goes through, and the locale is cp1252 on Windows: a reminder
    label, a fact about its owner or a line of a sweep report with an accent in it
    raised UnicodeEncodeError, which is a ValueError and so was not caught by the
    `except OSError` clauses around the callers either.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = staging(path)
    try:
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)   # gone already when the replace worked
    if mode is not None:
        os.chmod(path, mode)
    return path
