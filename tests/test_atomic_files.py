"""The files that are written by more than one process, under a name nobody shares.

A staging file with a fixed name is shared state: the assistant and the shell run
as separate processes, and a writer that installs the scratch file while another
writer is between the truncate and the write installs a truncated document. A
truncated document is not an error in all three of these readers, which is why
they are the ones checked:

    reminders.json    an empty list parses, so pending reminders disappear
    microphone.json   anything unreadable means on, so the microphone fails open
    config.local.json an empty object parses, so every switch is back to default

The interleaving is forced here rather than raced, so this is the same failure
every run. It is driven through the real `save()` of each module, with only
`Path.write_text` and `Path.replace` wrapped to hold one writer still at the exact
moment the other one is empty-handed.
"""
from pathlib import Path
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from archy import atomic, config, mic_state, privacy, reminders  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:56s} {detail}")


def parsed(text):
    """What a reader of one of these files would get, failures included.

    A test that raises instead of reporting cannot say what the failure was, and
    the failure here is exactly a file that does not parse.
    """
    try:
        return json.loads(text)
    except ValueError as exc:
        return f"<would not parse: {exc}>"


state = Path(tempfile.mkdtemp(prefix="archy-atomic-"))
config.STATE_DIR = state
reminders.PATH = state / "reminders.json"
config.LOCAL_CONFIG_FILE = state / "config.local.json"

real_write_text = Path.write_text
real_replace = os.replace


def torn(save, first_body, second_body, path):
    """Run two writers, and read the file at the moment the first one installs.

    Writer A stages its body and is stopped just before it moves it into place.
    Writer B then truncates its staging file and is stopped there, which is the
    state a writer is in halfway through a write. A is released, installs whatever
    the staging file holds at that moment, and the file is read while B is still
    stopped: with one shared staging name that is B's empty scratch file, which is
    the torn document, and with a name per writer it is A's own complete one.
    """
    started = threading.Event()
    truncated = threading.Event()
    release = threading.Event()
    a_thread = threading.get_ident()
    outcome = {}

    def write_text(self, body, *a, **k):
        if threading.get_ident() != a_thread:
            # the second writer, held between the truncate and the write
            with open(self, "w"):
                pass
            truncated.set()
            release.wait(10)
        return real_write_text(self, body, *a, **k)

    def replace(src, dst):
        if threading.get_ident() == a_thread:
            started.set()
            truncated.wait(10)
        return real_replace(src, dst)

    def second():
        started.wait(10)
        try:
            save(second_body)
        finally:
            outcome["second"] = True

    Path.write_text, os.replace = write_text, replace
    try:
        thread = threading.Thread(target=second)
        thread.start()
        save(first_body)
        try:
            outcome["at_install"] = path.read_text()
        except OSError as exc:
            outcome["at_install"] = f"<unreadable: {exc}>"
        release.set()
        thread.join(10)
    finally:
        Path.write_text, os.replace = real_write_text, real_replace
    outcome["after"] = path.read_text() if path.exists() else ""
    return outcome


print("-- reminders: a torn file reads as no reminders --")
items = [{"label": "call mum", "due": 1234.0, "repeat": None}]
outcome = torn(reminders.save, items, [], reminders.PATH)
final = reminders.PATH.read_text()
check("what is installed at the end is a whole document",
      parsed(final) == [], final[:40])
check("and the file at the moment of that install was whole as well",
      parsed(outcome["at_install"]) == items, outcome["at_install"][:40] or "<empty file>")
check("no staging file is left behind",
      not [p for p in state.iterdir() if p.name.endswith(".tmp")],
      str([p.name for p in state.iterdir()]))

print("-- the microphone: a torn file reads as on --")
# The reader is deliberately forgiving: a missing file is a fresh install, and a
# corrupt one is not a reason to sit there deaf. So a torn write here does not
# announce itself, it switches the microphone back on for an owner who turned it
# off because the room was not private.
mic_state.save(True)
mic = torn(mic_state.save, False, False, mic_state.path())
check("the file at the moment of the install is a whole document",
      parsed(mic["at_install"]) == {"enabled": False},
      mic["at_install"][:40] or "<empty file, which reads as on>")
check("and the reader is left with the switch off",
      mic_state.load() is False, "a torn file here would say on")
check("no staging file is left behind",
      not [p for p in state.iterdir() if p.name.endswith(".tmp")],
      str([p.name for p in state.iterdir()]))

print("-- the privacy switches: a torn file reads as a default --")
local = Path(config.LOCAL_CONFIG_FILE)
key = privacy.SWITCHES["memory"][0]
privacy.set_switch("memory", False)
privacy_run = torn(lambda value: privacy.set_switch("memory", value), True, False, local)
check("the file at the moment of the install is a whole object",
      isinstance(parsed(privacy_run["at_install"]), dict),
      privacy_run["at_install"][:40] or "<empty file, which reads as a default>")
check("and it carries the switch, not half of one",
      key in (parsed(privacy_run["at_install"]) if isinstance(
          parsed(privacy_run["at_install"]), dict) else {}),
      privacy_run["at_install"][:40] or "<empty file>")
check("no staging file is left behind",
      not [p for p in state.iterdir() if p.name.endswith(".tmp")],
      str([p.name for p in state.iterdir()]))

print("-- two writers never stage under one name --")
names = set()
lock = threading.Lock()


def stage_a_name():
    with lock:
        names.add(atomic.staging(reminders.PATH))


threads = [threading.Thread(target=stage_a_name) for _ in range(4)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(10)
check("the name carries the writer, so four writers have four names",
      len(names) == 4, f"{len(names)} distinct of 4")
check("and it names the file it is staging",
      all(name.name.startswith(".reminders.json.") for name in names),
      str(sorted(name.name for name in names)[0]))

print("-- what the old name would have done --")
# The name a fixed staging file used to have, and the reason the tests above are
# about a shared one: nothing stages there now, so a file left at that name by an
# older build cannot be installed over the real one by a later save.
stray = reminders.PATH.with_suffix(".tmp")
stray.write_text("half a doc")
reminders.save(items)
stray_text = stray.read_text() if stray.exists() else "<gone: a save moved it>"
check("a file at the old fixed name is not touched by a save",
      stray_text == "half a doc", stray_text)
check("and the real file is a whole document",
      parsed(reminders.PATH.read_text()) == items)
stray.unlink(missing_ok=True)

print()
print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
sys.exit(0 if not failed else 1)
