"""Reminders and timers: parsed locally, kept in a small file, spoken when due.

No cloud, no cron syntax. He says "remind me in twenty minutes to check the
oven" and twenty minutes later jarvis says so, out loud, even if the assistant
was restarted in between, because the due time lives on disk.
"""
import json
import re
import threading
import time

from . import config

PATH = config.STATE_DIR / "reminders.json"
_lock = threading.Lock()

# relative durations only; an alarm clock it is not
_UNIT = r"(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)"
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
# an absolute time is the one reminder shape this cannot serve, and the shape a
# model will happily agree to out loud. Better to say so plainly
_ABSOLUTE = re.compile(
    r"\b(at|by|around|before)\s+\d|\d\s*(am|pm)\b|\b(tomorrow|tonight|"
    r"noon|midnight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"\b|\bnext\s+(week|month|monday|tuesday|wednesday|thursday|friday)", re.I)


def wanted(text: str) -> bool:
    words = text.lower()
    if any(w in words for w in ("remind me", "reminder", "countdown")):
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
    if not _ABSOLUTE.search(words):
        return None
    return ("I can only set that from a delay, like in twenty minutes. "
            "Tell me how long and I will hold it.")


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


def parse(text: str) -> dict | None:
    """What he asked for: a set, a list, or a cancel. None if it is none of
    those, in which case the sentence goes to the model as usual."""
    words = text.lower().strip()
    if not any(w in words for w in ("remind me", "timer", "reminders", "reminder",
                                    "countdown")):
        return None
    if re.search(r"\b(cancel|clear|forget|stop)\b", words) and \
            any(w in words for w in ("reminder", "reminders", "timer")):
        return {"action": "cancel"}
    if re.search(r"\b(what|show|list|any|how long)\b", words) and \
            any(w in words for w in ("reminder", "reminders", "timer", "left")):
        return {"action": "list"}
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
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(items))
    tmp.replace(PATH)


def add(label: str, delay_s: float) -> dict:
    item = {"label": label, "due": time.time() + delay_s}
    with _lock:
        save(load() + [item])
    return item


def cancel() -> int:
    with _lock:
        items = load()
        save([])
    return len(items)


def due_items(now: float | None = None) -> list[str]:
    """Everything that has come due, removed from the file as it is returned."""
    now = now if now is not None else time.time()
    with _lock:
        items = load()
        ripe = [i for i in items if i.get("due", 0) <= now]
        if ripe:
            save([i for i in items if i.get("due", 0) > now])
    return [i["label"] for i in ripe]


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
    if abs(hours * 2 - round(hours * 2)) < 0.05 and hours >= 1.5:
        half = round(hours * 2) // 2
        return ("an hour and a half" if half == 1
                else f"{half} and a half hours")
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
        left = max(0, i.get("due", 0) - now)
        out.append(f"{i['label']} in {_spoken_gap(left)}")
    return out


GENERIC = ("Timer", "Reminder")       # labels that stand for nothing named


def confirm(action: dict) -> str:
    """What to say when the action is taken, before any model is involved."""
    if action["action"] == "cancel":
        n = cancel()
        if not n:
            return "There was nothing to cancel."
        return ("Done, that reminder is cleared." if n == 1
                else f"Done, {n} reminders cleared.")
    if action["action"] == "list":
        items = pending()
        if not items:
            return "Nothing pending."
        return "Pending: " + "; ".join(items[:4]) + "."
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
