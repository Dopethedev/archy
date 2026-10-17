"""Reminders: parsing, the durable store, and what happens when one comes due.

The store is a temp file, never the live one. Persistence is tested the way a
restart would meet it: a fresh read of the same file.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import jarvis.reminders as reminders
from jarvis import config

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


tmp = Path(tempfile.mkdtemp())
config.STATE_DIR = tmp
reminders.PATH = tmp / "reminders.json"

print("-- what counts as a reminder request --")
for text in ("remind me in 20 minutes to check the oven", "set a timer for 5 minutes",
             "hey jarvis, set a timer for ten minutes", "cancel my reminders",
             "what reminders do i have", "how long left on the timer"):
    check(f"wanted {text!r}", reminders.wanted(text))
for text in ("open the timer app", "remember my birthday", "what time is it"):
    check(f"not wanted {text!r}", not reminders.wanted(text))

print("-- parsing the sentence into an action --")
a = reminders.parse("remind me in 20 minutes to check the oven")
check("twenty minutes, with the label",
      a and a["action"] == "set" and abs(a["delay"] - 1200) < 0.01
      and a["label"] == "Check the oven", str(a))
a = reminders.parse("hey jarvis remind me in an hour to stand up")
check("'an hour' is an hour",
      a and a["action"] == "set" and abs(a["delay"] - 3600) < 0.01
      and a["label"] == "Stand up", str(a))
a = reminders.parse("remind me in half an hour")
check("half an hour with no task is still a reminder",
      a and abs(a["delay"] - 1800) < 0.01 and a["label"] == "Reminder", str(a))
a = reminders.parse("set a timer for 90 seconds")
check("a timer in seconds", a and abs(a["delay"] - 90) < 0.01
      and a["label"] == "Timer", str(a))

# halves and quarters, which are how people give a delay they have not counted
a = reminders.parse("remind me in an hour and a half")
check("an hour and a half is ninety minutes",
      a and abs(a["delay"] - 5400) < 0.01, str(a))
a = reminders.parse("remind me in two and a half hours")
check("two and a half hours is measured too",
      a and abs(a["delay"] - 9000) < 0.01, str(a))
a = reminders.parse("remind me in a quarter of an hour")
check("a quarter of an hour is fifteen minutes",
      a and abs(a["delay"] - 900) < 0.01, str(a))

# a name, and a sentence that must not become one
a = reminders.parse("set a timer for ten minutes called pasta")
check("a timer can be named", a and a["label"] == "Pasta", str(a))
a = reminders.parse("remind me to call mum in twenty minutes")
check("the time does not end up in the name",
      a and a["label"] == "Call mum", str(a))
# confirm() is the thing that takes the action, so it puts the reminder on disk:
# put the store back exactly as it was, or the checks below find an extra item
import json as _json
_before = _json.dumps(reminders.load())
_say = reminders.confirm(a) if a else ""
check("and the confirmation does not say it twice",
      _say and "Call mum in twenty minutes" not in _say, _say)
reminders.PATH.write_text(_before)
a = reminders.parse("remind me in 2 hours to check the download")
check("hours plural", a and abs(a["delay"] - 7200) < 0.01, str(a))
a = reminders.parse("cancel my reminders")
check("a cancel is a cancel", a and a["action"] == "cancel")
a = reminders.parse("what reminders do i have")
check("a query is a query", a and a["action"] == "list")
for text in ("remind me", "in 20 minutes", "remind me in 999 hours to never"):
    check(f"unparseable stays with the model: {text!r}", reminders.parse(text) is None)

# whisper writes small numbers as words about half the time, so a spoken
# "twenty minutes" has to mean the same as a written "20 minutes"
for text, want in (("remind me in twenty minutes", 1200.0),
                   ("remind me in twenty-five minutes to stretch", 1500.0),
                   ("remind me in twenty five minutes to stretch", 1500.0),
                   ("remind me in ninety seconds", 90.0),
                   ("remind me in two days to renew the thing", 172800.0),
                   ("remind me in three hours", 10800.0),
                   ("set a timer for ninety seconds", 90.0),
                   ("remind me in 90 s", 90.0)):
    a = reminders.parse(text)
    check(f"spoken numbers work: {text!r}",
          a and a["action"] == "set" and abs(a["delay"] - want) < 0.01, str(a))

# a request this cannot serve is one a model can only pretend to accept
for text in ("remind me at 5pm to call mum", "remind me tomorrow morning to call",
             "set an alarm for 7am", "remind me on friday to send the email"):
    check(f"an absolute time is refused here: {text!r}",
          reminders.parse(text) is None and reminders.cannot_do(text) is not None)
for text in ("remind me to buy milk", "what reminders do i have", "open the timer app"):
    check(f"no refusal where none is needed: {text!r}",
          reminders.cannot_do(text) is None)

print("-- the store survives a restart --")
reminders.add("Check the oven", 1.0)
reminders.add("Stand up", 3600.0)
fresh = json.loads(reminders.PATH.read_text())
check("both are on disk", len(fresh) == 2)
check("a due one comes back and leaves the file",
      reminders.due_items(now=time.time() + 2) == ["Check the oven"]
      and len(json.loads(reminders.PATH.read_text())) == 1)
check("the far one is still pending with a gap",
      "Stand up" in reminders.pending()[0] and "an hour" in reminders.pending()[0])
check("nothing due returns nothing", reminders.due_items() == [])

print("-- confirmations speak like a person --")
say = reminders.confirm({"action": "set", "delay": 1200, "label": "Check the oven"})
check("a set is confirmed with the gap", "20 minutes" in say and "Check the oven" in say)
say = reminders.confirm({"action": "cancel"})
check("a cancel reports the count", "cleared" in say)
say = reminders.confirm({"action": "cancel"})
check("an empty cancel says so", "nothing to cancel" in say)
say = reminders.confirm({"action": "list"})
check("an empty list says so", "Nothing pending" in say)

print("-- a due item is announced as a sentence --")
check("a nameless timer is a sentence",
      reminders.spoken("Timer") == "Your timer is up.", reminders.spoken("Timer"))
check("a nameless reminder is a sentence",
      reminders.spoken("Reminder") == "A reminder is due.")
check("a named one keeps its name",
      reminders.spoken("Check the oven") == "Reminder: Check the oven.")

print()
print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
sys.exit(0 if not failed else 1)
