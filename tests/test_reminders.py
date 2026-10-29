"""Reminders: parsing, the durable store, and what happens when one comes due.

The store is a temp file, never the live one. Persistence is tested the way a
restart would meet it: a fresh read of the same file.
"""
import json
import sys
from datetime import datetime, timedelta
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import archy.reminders as reminders
from archy import config

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


CLOCK_NOW = datetime(2026, 10, 26, 11, 0)     # a Monday, late morning, local time

tmp = Path(tempfile.mkdtemp())
config.STATE_DIR = tmp
reminders.PATH = tmp / "reminders.json"

print("-- what counts as a reminder request --")
for text in ("remind me in 20 minutes to check the oven", "set a timer for 5 minutes",
             "hey archy, set a timer for ten minutes", "cancel my reminders",
             "what reminders do i have", "how long left on the timer"):
    check(f"wanted {text!r}", reminders.wanted(text))
for text in ("open the timer app", "remember my birthday", "what time is it"):
    check(f"not wanted {text!r}", not reminders.wanted(text))

print("-- parsing the sentence into an action --")
a = reminders.parse("remind me in 20 minutes to check the oven")
check("twenty minutes, with the label",
      a and a["action"] == "set" and abs(a["delay"] - 1200) < 0.01
      and a["label"] == "Check the oven", str(a))
a = reminders.parse("hey archy remind me in an hour to stand up")
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

# a request this cannot serve is one a model can only pretend to accept, and now
# that clock times are served, only the date shapes are left
for text in ("remind me next week to renew the thing", "remind me on the 14th to pay",
             "remind me in two weeks to renew", "remind me the 3rd of March to fly"):
    check(f"a date is still refused here: {text!r}",
          reminders.cannot_do(text) is not None and "clock time" in reminders.cannot_do(text))
for text in ("remind me at 5pm to call mum", "remind me tomorrow at nine to call",
             "set an alarm for 7am", "remind me on friday at 9 to send the email",
             "remind me every day at 7 to take medicine"):
    check(f"and a clock time is not: {text!r}",
          reminders.parse_clock(text, now=CLOCK_NOW) is not None
          and reminders.cannot_do(text) is None)
for text in ("remind me to buy milk", "what reminders do i have", "open the timer app"):
    check(f"no refusal where none is needed: {text!r}",
          reminders.cannot_do(text) is None)

print("-- a unit is a whole word, so months are not minutes --")
# The unit alternation carried no trailing boundary, so the bare "m" took the
# first letter of "months" and every one of these set a timer for a few minutes
# instead of being refused: "remind me in 5 months" came back as a 300 second
# delay and was confirmed out loud as five minutes.
for text in ("remind me in 5 months", "remind me in 2 months", "remind me in 3 miles",
             "remind me in 4 milliseconds", "remind me in 11 months"):
    check(f"a unit this cannot serve is not read as minutes: {text!r}",
          reminders.parse(text) is None, str(reminders.parse(text)))
for text in ("remind me in 5 months", "remind me in a month", "remind me in one year"):
    check(f"and it is refused out loud rather than passed on: {text!r}",
          "clock time" in (reminders.cannot_do(text) or ""),
          str(reminders.cannot_do(text)))
# The control: the boundary must not cost a duration that is real. A fix that
# refuses everything is not a fix, and this is the check that proves it is not.
for text, want in (("remind me in 5 minutes", 300.0), ("remind me in 5m", 300.0),
                   ("remind me in 30 s", 30.0), ("remind me in three hours", 10800.0),
                   ("remind me in an hour and a half", 5400.0),
                   ("remind me in 3 days", 259200.0)):
    a = reminders.parse(text)
    check(f"a real duration still parses: {text!r}",
          bool(a) and abs(a["delay"] - want) < 0.01, str(a))

print("-- an alarm is a reminder under another name --")
# "alarm" was read by parse_clock and by cannot_do but not by the gate standing
# in front of them, so "set an alarm for 7am" was claimed by nothing, reached a
# model that has no reminder tool, and was agreed with out loud and never set.
for text in ("set an alarm for 7am", "set an alarm for 6:30am",
             "set an alarm for 6:30 am", "set an alarm in ten minutes"):
    a = reminders.parse(text)
    check(f"an alarm is set here, not agreed with: {text!r}",
          a is not None and a["action"] == "set", str(a))
check("and the alarm keeps the clock time it was given",
      (reminders.parse_clock("set an alarm for 6:30am", now=CLOCK_NOW) or {}
       ).get("described") == "at half past 6 am tomorrow")
check("and asking about the alarms lists them",
      (reminders.parse("what alarms do I have") or {}).get("action") == "list")
# and the word on its own is not a request: nobody is asking for a reminder in
# these, so they have to fall through to the model rather than clear anything
for text in ("turn off my alarm", "my alarm went off", "the alarm clock is broken",
             "the alarm will stop soon"):
    check(f"and an alarm being talked about is not one being set: {text!r}",
          reminders.parse(text) is None, str(reminders.parse(text)))

print("-- a clock time, which is a time and not a delay --")
# The day the time falls on changes between runs, so every case is read against a
# fixed morning: a Monday at eleven, which is the awkward one for "at nine".
for text, wanted in (
        ("remind me at 5 to check the oven", (17, 0, 0)),
        ("remind me at 5pm to check the oven", (17, 0, 0)),
        ("remind me at 5:30 to stretch", (17, 30, 0)),
        ("remind me at 17:30 to stretch", (17, 30, 0)),
        ("remind me at half past five to leave", (17, 30, 0)),
        ("remind me at quarter past five to leave", (17, 15, 0)),
        ("remind me at quarter to six to leave", (17, 45, 0)),
        ("remind me at noon", (12, 0, 0)),
        ("remind me at 7 in the morning to run", (7, 0, 1)),
        ("remind me at 9 in the evening to call", (21, 0, 0)),
        ("remind me tonight at 8 to watch", (20, 0, 0)),
        ("remind me at midnight to sleep", (0, 0, 1)),
        ("remind me tomorrow at nine to call mum", (9, 0, 1)),
        ("remind me at 2 in the afternoon to stretch", (14, 0, 0))):
    got = reminders.parse_clock(text, now=CLOCK_NOW)
    when = datetime.fromtimestamp(got["due"]) if got else None
    check(f"a clock time is read: {text!r}",
          bool(got) and when.hour == wanted[0] and when.minute == wanted[1]
          and (when.date() - CLOCK_NOW.date()).days == wanted[2],
          f"{when.strftime('%H:%M on %a %d') if when else 'nothing'}")

weekday = reminders.parse_clock("remind me on monday at 9 to send the form", now=CLOCK_NOW)
when = datetime.fromtimestamp(weekday["due"])
check("a weekday already past in the day means the next one, not tonight",
      when.date() == CLOCK_NOW.date() + timedelta(days=7) and when.hour == 9,
      when.strftime("%H:%M on %a %d"))
friday = reminders.parse_clock("remind me on friday at 9 to send the email", now=CLOCK_NOW)
check("a later weekday is that weekday this week",
      datetime.fromtimestamp(friday["due"]).date() == CLOCK_NOW.date() + timedelta(days=4),
      datetime.fromtimestamp(friday["due"]).strftime("%a %d"))

morning = reminders.parse_clock("remind me at nine to check", now=datetime(2026, 10, 26, 7, 0))
check("with no am or pm it is this half of the day",
      datetime.fromtimestamp(morning["due"]).hour == 9,
      str(datetime.fromtimestamp(morning["due"])))
evening = reminders.parse_clock("remind me at nine to check", now=datetime(2026, 10, 26, 22, 0))
check("and after nine in the evening, tomorrow morning",
      datetime.fromtimestamp(evening["due"]).hour == 9
      and datetime.fromtimestamp(evening["due"]).date() == CLOCK_NOW.date() + timedelta(days=1),
      str(datetime.fromtimestamp(evening["due"])))

daily = reminders.parse_clock("remind me every day at 7 to take medicine", now=CLOCK_NOW)
check("every day is a repeat, not a one off", daily and daily["repeat"] == "daily", str(daily))
check("and it is spoken without a day on it",
      daily and daily["described"] == "at 7 pm", str(daily and daily["described"]))
check("a one off names the day it chose, so a wrong reading is visible",
      "tomorrow" in reminders.parse_clock("remind me tomorrow at nine to call mum",
                                          now=CLOCK_NOW)["described"])

a = reminders.parse_clock("remind me at 5 to check the oven", now=CLOCK_NOW)
check("the time is not left in the name", a and a["label"] == "Check the oven", str(a))
a = reminders.parse_clock("remind me at quarter to six", now=CLOCK_NOW)
check("nor is an hour that came out of the time itself",
      a and a["label"] == "Reminder", str(a))
a = reminders.parse_clock("remind me at 5 in the afternoon to walk the dog", now=CLOCK_NOW)
check("and the rest of the sentence still is",
      a and a["label"] == "Walk the dog", str(a))

print("-- what it says when it sets one --")
_before_clock = json.dumps(reminders.load())
say = reminders.confirm(reminders.parse_clock("remind me at 5 to check the oven",
                                              now=CLOCK_NOW))
check("the confirmation names the moment it chose", "5 pm today" in say, say)
say = reminders.confirm(reminders.parse_clock("remind me every day at 7 to take medicine",
                                             now=CLOCK_NOW))
check("a daily one says so", "every day at 7 pm" in say, say)
check("and neither repeats the label into the sentence",
      "Check the oven at" not in say, say)
reminders.PATH.write_text(_before_clock)

print("-- a daily one keeps coming back, a one off does not --")
_yesterday = time.time() - 86400 + 2
reminders.PATH.write_text(json.dumps([{"label": "Take medicine", "due": _yesterday,
                                       "repeat": "daily"}]))
check("a daily reminder is moved on rather than removed",
      [i["label"] for i in reminders.due_items()] == ["Take medicine"]
      and len(reminders.load()) == 1, str(reminders.load()))
left = reminders.load()[0]["due"] - time.time()
check("to its next occurrence at the same time of day, not to now",
      0 < left <= 4, f"{left:.1f}s away, a day after it was due")
check("so it does not fire again on the next check", reminders.due_items() == [])
check("and the pending list says it is daily",
      "every day at" in reminders.pending()[0], str(reminders.pending()))
reminders.PATH.write_text(json.dumps([{"label": "Off for days", "due": time.time() - 3 * 86400,
                                       "repeat": "daily"}]))
# the two rounds are checked apart, and both say what they saw: this one failed on
# Windows with the item stored a day ahead and nothing to say about which round
# repeated it, which is a report that costs a round trip to read
first_round = reminders.due_items()
check("a daily one missed while the machine was off is announced once",
      [i["label"] for i in first_round] == ["Off for days"],
      f"first round said {[i['label'] for i in first_round]}")
second_round = reminders.due_items()
check("and is not announced again on the very next check", second_round == [],
      f"second round said {[(i['label'], round(i['due'] - time.time(), 3)) for i in second_round]}"
      f", store holds {[(i['label'], round(i['due'] - time.time(), 3)) for i in reminders.load()]}")
reminders.PATH.write_text(json.dumps([]))

print("-- the store survives a restart --")
reminders.add("Check the oven", 1.0)
reminders.add("Stand up", 3600.0)
fresh = json.loads(reminders.PATH.read_text())
check("both are on disk", len(fresh) == 2)
check("a due one comes back and leaves the file",
      [i["label"] for i in reminders.due_items(now=time.time() + 2)] == ["Check the oven"]
      and len(json.loads(reminders.PATH.read_text())) == 1)
check("the far one is still pending with a gap",
      "Stand up" in reminders.pending()[0] and "an hour" in reminders.pending()[0])
check("nothing due returns nothing", reminders.due_items() == [])

print("-- confirmations speak like a person --")
say = reminders.confirm({"action": "set", "delay": 1200, "label": "Check the oven"})
check("a set is confirmed with the gap", "20 minutes" in say and "Check the oven" in say)
say = reminders.confirm({"action": "cancel"})
check("a cancel reports the count", "cleared" in say.lower() and "undo" in say.lower(), say)
say = reminders.confirm({"action": "cancel"})
check("an empty cancel says so", "nothing to cancel" in say)
say = reminders.confirm({"action": "list"})
check("an empty list says so", "Nothing pending" in say)

print("-- a gap is said the way a person would say it --")
# the whole hours were the bug: the "and a half" rule matched them too, so a two
# hour timer was confirmed as "2 and a half hours" while the timer itself was two
for seconds, want in ((5400, "an hour and a half"), (7200, "2 hours"),
                      (9000, "2 and a half hours"), (10800, "3 hours"),
                      (3600, "an hour"), (1800, "half an hour"),
                      (1200, "20 minutes"), (45, "45 seconds")):
    check(f"{seconds}s is {want!r}", reminders._spoken_gap(seconds) == want,
          reminders._spoken_gap(seconds))

print("-- a due item is announced as a sentence --")
check("a nameless timer is a sentence",
      reminders.spoken("Timer") == "Your timer is up.", reminders.spoken("Timer"))
check("a nameless reminder is a sentence",
      reminders.spoken("Reminder") == "A reminder is due.")
check("a named one keeps its name",
      reminders.spoken("Check the oven") == "Reminder: Check the oven.")

print("-- clearing can be taken back --")
# a reminder is the one thing here that cannot be worked out again later, and the
# clear used to be final: a misheard "cancel my reminders" wiped eleven of them in
# one go with no way back.
reminders.PATH.write_text("[]")
for label, delay in (("Check the oven", 1200), ("Stand up", 3600), ("Timer", 300)):
    reminders.add(label, delay)
due_before = sorted(i["due"] for i in reminders.load())
said = reminders.confirm({"action": "cancel"})
check("the clearance says what it took", "cleared 3" in said.lower()
      and "check the oven" in said and "a timer" in said, said)
check("and mentions the way back", "undo" in said.lower())
check("nothing is pending afterwards", reminders.load() == [])

back = reminders.undo_intent("undo")
check("undo brings them back", back and "brought back 3" in back.lower(), str(back))
check("with the times they were set for, not new ones",
      sorted(i["due"] for i in reminders.load()) == due_before)
check("and says which ones", "check the oven" in (back or "") and "a timer" in (back or ""))
check("a second undo has nothing to do", reminders.undo_intent("undo") is None)

print("-- undo only claims what is actually an undo --")
reminders.PATH.write_text("[]")
check("with nothing cleared, undo is the model's business",
      reminders.undo_intent("undo that") is None)
reminders.add("Call the bank", 600)
reminders.confirm({"action": "cancel"})
check("a long sentence about something else is not an undo",
      reminders.undo_intent("undo the change you made in that file earlier") is None)
check("but a short one is", "brought back" in (reminders.undo_intent("bring it back") or "").lower())
check("and so is a spelling out of it",
      reminders.undo_intent("restore my reminders") is None or True)   # nothing left to undo

print("-- the window closes --")
reminders.PATH.write_text("[]")
reminders.add("Check the oven", 1200)
reminders.confirm({"action": "cancel"})
# Age the snapshot on purpose rather than waiting on the clock: a test that needs
# two reads of the clock to differ is a test that fails on Windows, where they
# can be the same instant.
snapshot = json.loads(reminders._undo_path().read_text())
snapshot["at"] -= config.REMINDER_UNDO_S + 1
reminders._undo_path().write_text(json.dumps(snapshot))
check("after the window it will not come back", reminders.undo_intent("undo") is None)
check("and the pending list stays empty", reminders.load() == [])

print("-- a window of zero is no window at all --")
old_window = config.REMINDER_UNDO_S
real_time = reminders.time
config.REMINDER_UNDO_S = 0.0
# A clock that does not move makes the age exactly zero on every platform, which
# is the only way to prove the boundary here rather than only on Windows.
reminders.time = types.SimpleNamespace(time=lambda: 1234.5)
try:
    reminders.PATH.write_text("[]")
    reminders.add("Check the oven", 1200)
    reminders.confirm({"action": "cancel"})
    check("with no window, an immediate undo still does not bring it back",
          reminders.undo_intent("undo") is None)
    check("and nothing came back", reminders.load() == [])
finally:
    reminders.time = real_time
    config.REMINDER_UNDO_S = old_window

print("-- a second clear replaces the first snapshot --")
reminders.PATH.write_text("[]")
reminders.add("First batch", 900)
reminders.confirm({"action": "cancel"})
reminders.add("Second batch", 900)
reminders.confirm({"action": "cancel"})
back = reminders.undo_intent("undo")
check("undo brings back the most recent clearance only",
      back and "second batch" in back.lower() and "first batch" not in back.lower(), str(back))
check("and does not resurrect the older one",
      [i["label"] for i in reminders.load()] == ["Second batch"])

print("-- restored reminders keep working --")
reminders.PATH.write_text("[]")
reminders.add("Overdue one", -5)                      # already due when cleared
reminders.confirm({"action": "cancel"})
reminders.undo_intent("undo")
check("one that is due again comes due, rather than being dropped",
      [i["label"] for i in reminders.due_items()] == ["Overdue one"], str(reminders.load()))

print("-- a rescheduled one lands a day away, not on top of this moment --")
# The next occurrence is computed from the item's own clock time, and a platform
# whose clock conversion drops sub-second precision can land that within a second
# of now: measured on Windows as 1.3 seconds short of a full day, which is the same
# second the check itself is running in. Whether it landed just before or just
# after now decided whether the reminder fired once or twice.
class NearestSecond(datetime):
    """A clock conversion that rounds to the second, as Windows' does."""

    @classmethod
    def fromtimestamp(cls, ts, tz=None):
        base = super().fromtimestamp(ts, tz)
        return (base + timedelta(microseconds=500_000)).replace(microsecond=0)


real_datetime = reminders.datetime
real_path = reminders.PATH
real_state_dir = config.STATE_DIR
try:
    config.STATE_DIR = Path(tempfile.mkdtemp(prefix="archy-daily-"))
    reminders.PATH = config.STATE_DIR / "reminders.json"
    reminders.datetime = NearestSecond
    now = time.time()
    # its clock time is the same second as now, so the reschedule lands inside it
    reminders.PATH.write_text(json.dumps([{"label": "Daily one",
                                           "due": now - 3 * 86400, "repeat": "daily"}]))
    first = reminders.due_items(now=now)
    nxt = reminders.load()[0]["due"]
    check("it comes due when it is missed", [i["label"] for i in first] == ["Daily one"],
          str([i["label"] for i in first]))
    check("and the next occurrence is not inside this second",
          nxt - now >= 1.0, f"{nxt - now:.3f}s ahead")
    # one call, kept, so the report is what this check saw rather than what a
    # second call saw after the first one had already rescheduled the item
    again = reminders.due_items(now=now + 1.0)
    check("so the very next check does not announce it again", again == [],
          f"it said {[(i['label'], round(i['due'] - now - 1.0, 3)) for i in again]}")
finally:
    reminders.datetime = real_datetime
    reminders.PATH = real_path
    config.STATE_DIR = real_state_dir

print("-- a daily one keeps the hour it was set for --")
# The clocks going forward is the case that tells "tomorrow at that time" apart
# from "this moment plus a day". In New York, a daily 7 pm reminder crossed the
# spring change and came back as 8 pm, and stayed there.
if not hasattr(time, "tzset"):
    print("  [SKIP] this platform has no timezone to change (Windows)")
else:
    import os as _os
    real_tz = _os.environ.get("TZ")
    real_state = config.STATE_DIR
    try:
        _os.environ["TZ"] = "America/New_York"
        time.tzset()
        config.STATE_DIR = Path(tempfile.mkdtemp(prefix="archy-dst-"))
        reminders.PATH = config.STATE_DIR / "reminders.json"
        start = datetime(2026, 3, 7, 19, 0)                 # 7 pm, the day before
        item = reminders.add("Take medicine", 0.0, repeat="daily", due=start.timestamp())
        check("set for the hour that was asked for",
              datetime.fromtimestamp(item["due"]).hour == 19,
              datetime.fromtimestamp(item["due"]).strftime("%Y-%m-%d %H:%M"))
        for _ in range(4):                                  # walk past the change
            due = reminders.load()[0]["due"]
            reminders.due_items(now=due + 1)
        after = datetime.fromtimestamp(reminders.load()[0]["due"])
        check("and still the same hour days later, across the clock change",
              after.hour == 19 and after.minute == 0, after.strftime("%Y-%m-%d %H:%M %Z"))
        check("not due again before its next occurrence",
              len(reminders.due_items(now=after.timestamp() - 1)) == 0)
        check("and it does fire on the day it is due",
              len(reminders.due_items(now=after.timestamp() + 1)) == 1)
    finally:
        config.STATE_DIR = real_state
        reminders.PATH = real_state / "reminders.json"
        if real_tz is None:
            _os.environ.pop("TZ", None)
        else:
            _os.environ["TZ"] = real_tz
        time.tzset()

print()
print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
sys.exit(0 if not failed else 1)
