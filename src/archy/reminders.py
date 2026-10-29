"""Reminders and timers: parsed locally, kept in a small file, spoken when due.

No cloud, no cron syntax. He says "remind me in twenty minutes to check the
oven" and twenty minutes later archy says so, out loud, even if the assistant
was restarted in between, because the due time lives on disk.
"""
import json
import re
from datetime import datetime, time as dtime, timedelta
import threading
import time

from . import atomic, config

PATH = config.STATE_DIR / "reminders.json"
_lock = threading.Lock()

# relative durations only; an alarm clock it is not.
# The lookahead is the one _WORD already carries, for the same reason: without it
# the alternation matched a prefix of a longer word, so the "m" took the first
# letter of "months" and "remind me in five months" set a five minute timer.
_UNIT = r"(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])"
# whisper writes small numbers as words about half the time, and "remind me in
# twenty minutes" used to fall through to the model, which has no reminder tool
# and would either ask again or agree to something it cannot do
_ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
         "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
# the lookahead keeps the alternation from matching a prefix of a longer word:
# without it "forty" would match inside "forty-five" and then fail on the
# remainder, and "ten" inside "tennis"
_WORD = rf"(?:{'|'.join(list(_ONES) + list(_TENS))})(?![a-z])"
_COMPOUND = rf"(?:{_WORD})(?:[\s-]{_WORD})?"
_AND_HALF = r"(?:\s+and\s+a\s+(?:half|quarter))"
_AMOUNT = (rf"(?:an?|half\s+an?|a\s+quarter(?:\s+of\s+an?)?"
           rf"|{_COMPOUND}{_AND_HALF}?|\d+)")
# The hours people say, next to the digits they write.
_WORD_HOURS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
               "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
               "twelve": 12}
_RE_IN = re.compile(
    rf"\bin\s+({_AMOUNT})\s*{_UNIT}", re.I)
_TO = re.compile(r"\bto\s+(?:remember\s+)?(.+?)[.?!]?\s*$", re.I)
_NAMED = re.compile(r"\b(?:called|named)\s+(.+?)[.?!]?\s*$", re.I)
# "remind me to call mum in twenty minutes" is a sentence, and the twenty
# minutes belongs to the reminder rather than to its name: without this the
# timer is called "call mum in twenty minutes" and the confirmation says
# "Done, in 20 minutes: Call mum in twenty minutes."
_DURATION_IN_LABEL = re.compile(
    rf"\s+(?:in|at|after|for)\s+{_AMOUNT}\s*{_UNIT}\b.*$", re.I)
_TIMER = re.compile(rf"(?:set\s+)?(?:a\s+)?timer\s+for\s+({_AMOUNT})\s*"
                    + _UNIT, re.I)
_MINUTE_WORDS = ("minute", "minutes", "mins", "min", "m")
_HOUR_WORDS = ("hour", "hours", "hrs", "hr", "h")
_DAY_WORDS = ("day", "days", "d")
# A clock time: "at five", "at 5:30", "at 5pm", "at noon", "at half past five".
def wanted(text: str) -> bool:
    words = text.lower()
    if any(w in words for w in ("remind me", "reminder", "countdown", "alarm")):
        return True
    # "timer" alone appears in ordinary sentences like "open the timer app",
    # so it counts only with an actual timer verb or duration beside it
    if "timer" in words and re.search(
            r"(set|cancel|start|stop|for\s+(an?\s+|half\s+|\d+)|how long|left)", words):
        return True
    return False


def cannot_do(text: str) -> str | None:
    """A reminder request this cannot serve, said plainly instead of passed on.

    Only when the request names an absolute time or day. The model has no
    reminder tool, so a request it cannot serve is a request it can only appear
    to accept: "remind me at five" would come back as a confident yes and then
    never fire. Asking for a delay instead is honest, and short.
    """
    words = text.lower()
    if not any(w in words for w in ("remind", "reminder", "timer", "alarm")):
        return None
    if not _UNSUPPORTED.search(words):
        return None
    return ("I can do a clock time, a delay, or every day at a time, but not a date "
            "like that. Give me an hour and I will hold it.")


def _number(amount: str) -> float | None:
    """A count that may have been written as digits or as words."""
    text = amount.strip().lower()
    if text.startswith("half"):
        return 0.5
    if text.startswith("a quarter") or text.startswith("quarter"):
        return 0.25
    # "two and a half hours": the half belongs to this count
    frac = 0.0
    m = re.search(r"and\s+a\s+(half|quarter)", text)
    if m:
        frac = 0.25 if m.group(1) == "quarter" else 0.5
        text = text[:m.start()].strip()
        if not text:
            return None
    if text in ("a", "an"):
        return 1.0
    digits = re.sub(r"[^0-9.]", "", text)
    if digits:
        try:
            return float(digits)
        except ValueError:
            return None
    total = 0
    for word in re.split(r"[-\s]+", text):
        if word in _ONES:
            total += _ONES[word]
        elif word in _TENS:
            total += _TENS[word]
        else:
            return None
    return (float(total) + frac) if total else None


def _seconds(amount: str, unit: str) -> float | None:
    n = _number(amount)
    if n is None:
        return None
    unit = unit.lower()
    if unit in _DAY_WORDS:
        return n * 86400.0
    if unit in _HOUR_WORDS:
        return n * 3600.0
    if unit in _MINUTE_WORDS:
        return n * 60.0
    return n                                    # seconds


def _first_match(text: str, pattern: re.Pattern):
    """The match, so the caller can look at what follows the duration too."""
    return pattern.search(text)


# ------------------------------------------------------------ clock times
_RE_AT = re.compile(
    r"\b(?:at|by|around|about|before)\s+"
    r"(?:(?P<hour>\d{1,2}|" + "|".join(_WORD_HOURS) + r")"
    r"(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm|a\.m\.|p\.m\.)?"
    r"|(?P<named>noon|midnight)"
    r"|half\s+past\s+(?P<half>\d{1,2}|" + "|".join(_WORD_HOURS) + r")"
    r"|quarter\s+(?P<quarter>past|to)\s+(?P<quarter_hour>\d{1,2}|"
    + "|".join(_WORD_HOURS) + r"))", re.I)
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6}
_RE_WEEKDAY = re.compile(r"\b(?:(next)\s+)?(" + "|".join(_WEEKDAYS) + r")\b", re.I)
_RE_DAY_WORD = re.compile(r"\b(tomorrow|tonight|today|this evening|this morning|"
                          r"this afternoon)\b", re.I)
_RE_EVENING = re.compile(r"\b(evening|night|tonight|afternoon)\b", re.I)
_RE_MORNING = re.compile(r"\b(morning|breakfast)\b", re.I)
_RE_DAILY = re.compile(r"\b(every\s*day|everyday|daily|each\s+day|every\s+morning|"
                       r"every\s+night)\b", re.I)
# Shapes still not served. A model agrees to these out loud and then never fires
# them, so they get a plain sentence instead of a confident yes.
# The units a delay cannot be measured in. Weeks were here and months and years
# were not, so a request in months was read by the delay parser as minutes. "a"
# and "one" are in the count because "remind me in a month" is the same request
# as "remind me in two months".
_UNSERVED = (r"(?:\d+|a\s+couple\s+of|an?|one|two|three|four|five|six|seven|eight|"
             r"nine|ten|eleven|twelve)")
_UNSUPPORTED = re.compile(
    rf"\bnext\s+(week|month|year|weekend)\b|\bon\s+the\s+\d"
    r"|\bthe\s+\d+(?:st|nd|rd|th)\s+of\b"
    rf"|\b{_UNSERVED}\s*(?:weeks?|months?|years?)\b", re.I)
# "set an alarm for 7am" has no "at" in it, and the am is what makes it a clock time.
# The minutes are optional here for the same reason they are in _RE_AT: "set an
# alarm for 6:30am" is the shape an alarm is usually set in, and without them the
# sentence was read as no time at all.
_RE_BARE_AMPM = re.compile(
    r"\b(?P<hour>\d{1,2}|" + "|".join(_WORD_HOURS) + r")\s*"
    r"(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm|a\.m\.|p\.m\.)",
    re.I)


def _spoken_hour(hour: int, minute: int) -> str:
    """A time as a person says it out loud, because it is spoken back as it is set."""
    if minute == 0 and hour == 0:
        return "midnight"
    if minute == 0 and hour == 12:
        return "noon"
    suffix = "am" if hour < 12 else "pm"
    twelve = hour % 12 or 12
    if minute == 0:
        return f"{twelve} {suffix}"
    if minute == 30:
        return f"half past {twelve} {suffix}"
    if minute == 15:
        return f"quarter past {twelve} {suffix}"
    if minute == 45:
        return f"quarter to {(twelve % 12) + 1} {suffix}"
    return f"{twelve}:{minute:02d} {suffix}"


def _day_phrase(when: datetime, now: datetime) -> str:
    days = (when.date() - now.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if 2 <= days <= 6:
        return when.strftime("%A")
    return f"{when.strftime('%A')} the {when.day}"


def _describe(when: datetime, now: datetime | None = None) -> str:
    """The moment as it will be spoken back, day included unless it is every day."""
    if now is None:
        return f"at {_spoken_hour(when.hour, when.minute)}"
    return f"at {_spoken_hour(when.hour, when.minute)} {_day_phrase(when, now)}"


_CLOCK_WORDS = set(_WORD_HOURS) | set(_WEEKDAYS) | {
    "at", "to", "past", "half", "quarter", "in", "on", "a", "an", "and", "every",
    "day", "daily", "today", "tomorrow", "tonight", "noon", "midnight", "am", "pm",
    "this", "morning", "evening", "afternoon", "night", "around", "about", "by",
    "before", "o", "clock", "for", "remind", "reminder", "me", "please", "its", "it"}


def _strip_clock_words(text: str) -> str:
    """What is left of a sentence once the time is taken out of it.

    "remind me at quarter to six" leaves nothing, and an empty label reads better as
    "Reminder" than as "Six": the six is the hour, not the thing being remembered.
    """
    text = re.sub(r"\b(in|at|this)\s+the\s+(morning|evening|afternoon|night)\b", " ",
                  text, flags=re.I)
    text = re.sub(r"\bthis\s+(morning|evening|afternoon)\b", " ", text, flags=re.I)
    text = re.sub(r"\bo'?\s?clock\b", " ", text, flags=re.I)
    kept = [word for word in re.split(r"\s+", text.strip(" ,.-")) if word
            and word.lower().strip(".,!?") not in _CLOCK_WORDS]
    return " ".join(kept).strip(" ,.-")


def _hour_of(value: str) -> int | None:
    value = (value or "").strip().lower()
    if value.isdigit():
        return int(value)
    return _WORD_HOURS.get(value)


def _forced_meridiem(words: str) -> str | None:
    """"in the evening" and "tonight" are not ambiguous, so they decide it."""
    if _RE_EVENING.search(words):
        return "pm"
    if _RE_MORNING.search(words):
        return "am"
    return None


def _clock_target(hour: int, minute: int, meridiem: str | None, day_offset: int,
                  now: datetime, same_weekday: bool = False) -> datetime | None:
    """The next moment at that clock time, on or after the day asked for.

    With no am or pm the answer is this half of the day: someone setting an evening
    reminder says "at seven" meaning the evening. The sentence that confirms it names
    the time it chose, so a wrong reading is visible at the moment it is set.
    """
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    base = now.date() + timedelta(days=day_offset)
    if meridiem:
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
        target = datetime.combine(base, dtime(hour=hour, minute=minute))
        if target <= now:
            target += timedelta(days=7 if same_weekday else 1)
        return target
    if hour > 12:                                   # a 24 hour clock time
        target = datetime.combine(base, dtime(hour=hour, minute=minute))
        if target <= now:
            target += timedelta(days=7 if same_weekday else 1)
        return target
    prefer_pm = now.hour >= 12
    first = hour + 12 if prefer_pm else hour
    second = hour if prefer_pm else hour + 12
    if same_weekday:
        # "on Monday at nine" said on a Monday, once nine has gone, means the next
        # Monday at nine rather than nine tonight: the day was named for a reason.
        target = datetime.combine(base, dtime(hour=first, minute=minute))
        return target if target > now else target + timedelta(days=7)
    target = datetime.combine(base, dtime(hour=first, minute=minute))
    if target <= now:
        target = datetime.combine(base, dtime(hour=second, minute=minute))
    if target <= now:
        target += timedelta(days=1)
    return target


def parse_clock(text: str, now: datetime | None = None) -> dict | None:
    """A reminder at a clock time: "at 5pm", "tomorrow at nine", "every day at 7"."""
    words = text.lower().strip()
    if not any(w in words for w in ("remind", "reminder", "timer", "alarm")):
        return None
    now = now or datetime.now()
    found = _RE_AT.search(words) or _RE_BARE_AMPM.search(words)
    if not found:
        return None
    # read by name, because the two patterns that find a time do not carry the same
    # groups: "at five" has an hour, "5pm" has one and a meridiem, "at noon" has
    # neither, and asking for a group that is not there is an error, not a None
    groups = found.groupdict()
    named_meridiem = None
    if groups.get("named"):
        if groups["named"] == "noon":
            hour, minute, named_meridiem = 12, 0, "pm"
        else:
            hour, minute, named_meridiem = 0, 0, "am"
    elif groups.get("half"):
        hour, minute = _hour_of(groups["half"]), 30
    elif groups.get("quarter"):
        hour = _hour_of(groups["quarter_hour"])
        if groups["quarter"] == "to":
            hour, minute = hour - 1, 45
        else:
            minute = 15
    else:
        hour, minute = _hour_of(groups.get("hour")), int(groups.get("minute") or 0)
    if hour is None:
        return None

    meridiem = groups.get("meridiem") or named_meridiem or _forced_meridiem(words)
    day_offset, same_weekday = 0, False
    weekday = _RE_WEEKDAY.search(words)
    day_word = _RE_DAY_WORD.search(words)
    if weekday:
        wanted = _WEEKDAYS[weekday.group(2).lower()]
        ahead = (wanted - now.weekday()) % 7
        if weekday.group(1):                      # "next monday" is the week after
            ahead = ahead + 7 if ahead else 7
        day_offset, same_weekday = ahead, ahead == 0
    elif day_word:
        word = day_word.group(1)
        day_offset = 1 if word == "tomorrow" else 0
        if word == "tonight":
            meridiem = meridiem or "pm"

    target = _clock_target(hour, minute, meridiem, day_offset, now, same_weekday)
    if target is None:
        return None
    repeat = "daily" if _RE_DAILY.search(words) else None
    if repeat and target <= now:
        target += timedelta(days=1)

    label_match = _NAMED.search(text) or _TO.search(text)
    label = ""
    if label_match:
        label = _DURATION_IN_LABEL.sub("", label_match.group(1).strip())
        label = _RE_AT.sub("", label)
        label = _strip_clock_words(label)
    if not label:
        label = "Timer" if "timer" in words else "Reminder"
    label = label[0].upper() + label[1:] if label and label[0].isalpha() else label
    return {"action": "set", "due": target.timestamp(), "label": label[:120],
            "repeat": repeat,
            "described": _describe(target, None if repeat else now)}


def parse(text: str) -> dict | None:
    """What he asked for: a set, a list, or a cancel. None if it is none of
    those, in which case the sentence goes to the model as usual."""
    words = text.lower().strip()
    # "alarm" belongs in this list: parse_clock and cannot_do both already read
    # it, and this line is the only way into them, so without it "set an alarm for
    # 7am" was claimed by nothing and reached a model with no reminder tool to
    # call. Two gates out of four knew the word and two did not.
    if not any(w in words for w in ("remind me", "timer", "reminders", "reminder",
                                    "countdown", "alarm")):
        return None
    if re.search(r"\b(cancel|clear|forget|stop)\b", words) and \
            any(w in words for w in ("reminder", "reminders", "timer")):
        return {"action": "cancel"}
    # "alarms" is in this one and not in the cancel above on purpose: a sentence
    # with the word alarm in it and the word stop in it is usually about an alarm
    # clock going off, and clearing everything on that reading is the one mistake
    # here that takes something away.
    if re.search(r"\b(what|show|list|any|how long)\b", words) and \
            any(w in words for w in ("reminder", "reminders", "timer", "left",
                                     "alarm", "alarms")):
        return {"action": "list"}
    at = parse_clock(text)
    if at:
        return at
    m = _first_match(words, _RE_IN) or _TIMER.search(words)
    if not m:
        return None
    pair = (m.group(1), m.group(2))
    _match_end = m.end()
    delay = _seconds(pair[0].strip(), pair[1])
    if delay is None:
        return None
    # the other way round: "an hour and a half" puts the half after the unit, and
    # reading only the hour fires it half an hour early, which is worse than not
    # understanding it at all
    m_after = re.match(rf"\s*{_AND_HALF}", text[_match_end:])
    if m_after:
        delay += (0.25 if "quarter" in m_after.group(0) else 0.5) * (_seconds("1", pair[1]) or 0.0)
    if not delay or delay <= 0 or delay > 7 * 86400:
        return None
    # A timer nobody named still has to be spoken about, and "Your timer" as a
    # label reads like a machine in a list and in a sentence. The general ones
    # are named for what they are, and the sentences are phrased around them.
    label_m = _NAMED.search(text) or _TO.search(text)
    if label_m:
        label = _DURATION_IN_LABEL.sub("", label_m.group(1).strip()).strip(" ,.-")
    elif re.search(r"timer", words):
        label = "Timer"
    else:
        label = "Reminder"
    label = label[0].upper() + label[1:] if label and label[0].isalpha() else label
    return {"action": "set", "delay": delay, "label": label[:120]}


# ------------------------------------------------------------------ the store
def load() -> list[dict]:
    try:
        data = json.loads(PATH.read_text())
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save(items: list[dict]):
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Not a fixed "reminders.tmp": two writers share that name, and a reminder
    # list installed while the other writer was halfway through writing it reads
    # as no reminders at all.
    atomic.write(PATH, json.dumps(items))


def add(label: str, delay_s: float, repeat: str | None = None,
        due: float | None = None) -> dict:
    """A reminder that fires once at a moment, or every day at a clock time."""
    item = {"label": label, "due": float(due) if due is not None else time.time() + delay_s}
    if repeat:
        item["repeat"] = repeat
    with _lock:
        save(load() + [item])
    return item


def _undo_path():
    """Where the last clearance is kept. Read lazily so a test that moves the
    state directory does not write into the real one."""
    return config.STATE_DIR / "reminders-undo.json"


def _snapshot(items: list[dict]) -> None:
    """Keep what is about to be cleared, so a misheard cancel is not final.

    A reminder is the one thing here that cannot be worked out again afterwards:
    the whole reason it was set is that it would be forgotten. Clearing is
    immediate, because waiting for a confirmation on a one-thing-at-a-time
    assistant is worse than the problem, so the answer is to make it reversible
    for a short while instead.
    """
    try:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        _undo_path().write_text(json.dumps({"at": time.time(), "items": items}))
    except OSError:
        pass                    # it was cleared either way; this only adds the way back


def cancel_all() -> tuple[int, list[str]]:
    """Clear everything pending. Returns the count and the labels it took away."""
    with _lock:
        items = load()
        if items:
            _snapshot(items)
        save([])
    return len(items), [i.get("label", "") for i in items]


def cancel() -> int:
    return cancel_all()[0]


_UNDO = re.compile(
    r"(?:\bundo\b|\bun-?cancel\b"
    r"|\b(?:bring|put)\s+(?:them|those|it|my\s+reminders?|the\s+reminders?)\s+back\b"
    r"|\bbring\s+back\s+(?:my\s+|the\s+)?(?:reminders?|timers?)\b"
    r"|\brestore\s+(?:my\s+|the\s+)?(?:reminders?|timers?|alarms?)\b)", re.I)


def undo_intent(text: str) -> str | None:
    """Bring back the last clearance, if this asks for it and it was recent.

    Only claims the sentence when there is something to bring back, so "undo"
    aimed at something else stays the model's business. A short sentence is
    enough to mean it; a long one has to say reminders, because "undo the change
    you made in that file" is a different request and restoring a timer instead
    would be maddening.
    """
    words = text.lower().split()
    if len(words) > 6 and not any(w.startswith(("remind", "timer", "alarm"))
                                 for w in words):
        return None
    if not _UNDO.search(text):
        return None
    path = _undo_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    age = time.time() - float(data.get("at") or 0)
    items = data.get("items") or []
    # A window of zero is no window at all, and the comparison is inclusive for a
    # reason: a clock that ticks sixty-four times a second, which is what Windows
    # has, can return the same instant for the snapshot and for the undo, making
    # the age exactly zero.
    if not items or age >= config.REMINDER_UNDO_S:
        path.unlink(missing_ok=True)        # too late, or nothing worth keeping
        return None
    with _lock:
        save(load() + items)                # their original times, not new ones
    path.unlink(missing_ok=True)
    n = len(items)
    return (f"Brought back one reminder: {_spoken_names([i.get('label','') for i in items])}."
            if n == 1 else
            f"Brought back {n} reminders: {_spoken_names([i.get('label','') for i in items])}.")


def _spoken_names(labels: list[str]) -> str:
    """The labels as someone would say them out loud."""
    named = []
    for label in labels:
        label = (label or "").strip()
        if not label:
            continue
        named.append("a " + label.lower() if label in GENERIC
                     else label[:1].lower() + label[1:])
    if not named:
        return "something"
    if len(named) == 1:
        return named[0]
    if len(named) > 3:
        return ", ".join(named[:3]) + f" and {len(named) - 3} more"
    return ", ".join(named[:-1]) + " and " + named[-1]


def due_items(now: float | None = None) -> list[dict]:
    """Everything that has come due, removed from the file as it is returned.

    A daily one is not removed but moved on to its next occurrence, so a machine
    that was off for three days announces it once when it comes back rather than
    three times, and then keeps to its clock instead of drifting.

    "Keeps to its clock" is the part that needs care: the next occurrence is that
    clock time on the next day, not this moment plus twenty four hours. Those are
    the same thing until the clocks change, and then they are an hour apart for
    good: measured in New York across the spring change, a daily reminder set for
    7 pm was rescheduled to 8 pm and stayed there. Adding a day to the local date
    and reading the clock time off it is what keeps the hour where it was put.
    """
    now = now if now is not None else time.time()
    with _lock:
        items = load()
        ripe = [i for i in items if i.get("due", 0) <= now]
        if ripe:
            kept = []
            for item in items:
                if item.get("due", 0) > now:
                    kept.append(item)
                elif item.get("repeat"):
                    when = datetime.fromtimestamp(item["due"])
                    nxt = item["due"]
                    while nxt <= now:
                        when += timedelta(days=1)
                        nxt = when.timestamp()
                    if nxt - now < 1.0:
                        # Landed on top of this moment, so the very next check a
                        # second later would find it due again and announce it
                        # twice. Measured on Windows, whose clock conversion drops
                        # sub-second precision: the next occurrence came out
                        # somewhere within a second of now, and whether it was
                        # before or after decided whether the reminder fired once
                        # or twice. A daily reminder is a day ahead, not a moment
                        # ahead.
                        when += timedelta(days=1)
                        nxt = when.timestamp()
                    kept.append({**item, "due": nxt})
            save(kept)
    return ripe


def _spoken_gap(seconds: float) -> str:
    """A duration as something a person would say out loud.

    3600 seconds is an hour, not sixty minutes, and a half hour is a half hour
    rather than thirty minutes. Both the confirmation and the list of pending
    reminders read this, so they cannot drift apart.
    """
    if seconds <= 90:
        return f"{seconds:.0f} seconds"
    minutes = seconds / 60
    if abs(minutes - 30) <= 2:
        return "half an hour"
    if abs(minutes - 60) <= 4:
        return "an hour"
    if minutes < 90:
        return f"{minutes:.0f} minutes"
    hours = minutes / 60
    if hours >= 20:
        days = hours / 24
        if abs(days - round(days)) < 0.05:
            days = round(days)
            return "a day" if days == 1 else f"{days} days"
        return f"{days:.1f} days"
    # "and a half" only for actual halves. The old test was "twice the hours is
    # close to a whole number", which is also true of every whole hour, so a two
    # hour timer was confirmed out loud as "2 and a half hours" while being set
    # for two. The timer was right; the sentence about it was not.
    half = round(hours * 2) / 2
    if half % 1 == 0.5 and abs(hours - half) < 0.05 and half >= 1.5:
        return ("an hour and a half" if half == 1.5
                else f"{int(half)} and a half hours")
    if abs(hours - round(hours)) < 0.1:
        hours = round(hours)
        return "an hour" if hours == 1 else f"{hours:.0f} hours"
    return f"{hours:.1f} hours"


def pending() -> list[str]:
    """The not-yet-due, soonest first, with a human gap."""
    now = time.time()
    items = sorted(load(), key=lambda i: i.get("due", 0))
    out = []
    for i in items:
        due = i.get("due", 0)
        if i.get("repeat"):
            when = datetime.fromtimestamp(due)
            out.append(f"{i['label']} every day at {_spoken_hour(when.hour, when.minute)}")
            continue
        left = max(0, due - now)
        out.append(f"{i['label']} in {_spoken_gap(left)}")
    return out


GENERIC = ("Timer", "Reminder")       # labels that stand for nothing named


def confirm(action: dict) -> str:
    """What to say when the action is taken, before any model is involved."""
    if action["action"] == "cancel":
        n, labels = cancel_all()
        if not n:
            return "There was nothing to cancel."
        return (f"Cleared one: {_spoken_names(labels)}. Say undo if you want it back."
                if n == 1 else
                f"Cleared {n}: {_spoken_names(labels)}. Say undo if you want them back.")
    if action["action"] == "list":
        items = pending()
        if not items:
            return "Nothing pending."
        return "Pending: " + "; ".join(items[:4]) + "."
    if action["action"] == "set" and action.get("due") is not None:
        add(action["label"], 0.0, repeat=action.get("repeat"), due=action["due"])
        when = action.get("described", "")
        daily = action.get("repeat") is not None
        if daily:
            when = f"every day {when}"
        if action["label"] in GENERIC:
            return (f"Done, {when}." if action["label"] == "Reminder"
                    else f"Done, a timer {when}.")
        return f"Done, {when}: {action['label']}."
    if action["action"] == "set":
        add(action["label"], action["delay"])
        gap = _spoken_gap(action["delay"])
        if action["label"] in GENERIC:
            # nothing was named, so say the time and stop: "Done, in twenty
            # minutes: Timer." is the kind of sentence that gives it away
            return f"Done, in {gap}." if action["label"] == "Reminder" \
                else f"Done, a timer for {gap}."
        return f"Done, in {gap}: {action['label']}."
    return ""


def spoken(label: str) -> str:
    """How a due item is announced. General ones are a sentence, not a label."""
    if label == "Timer":
        return "Your timer is up."
    if label == "Reminder":
        return "A reminder is due."
    return f"Reminder: {label}."
