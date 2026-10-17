"""Reminders and timers: parsed locally, kept in a small file, spoken when due.

No cloud, no cron syntax. He says "remind me in twenty minutes to check the
oven" and twenty minutes later jarvis says so, out loud, even if the assistant
was restarted in between, because the due time lives on disk.
"""
import json
import re
import threading
import time
from pathlib import Path

from . import config

PATH = config.STATE_DIR / "reminders.json"
_lock = threading.Lock()

# relative durations only; an alarm clock it is not
_UNIT = r"(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?)"
_RE_IN = re.compile(
    r"\bin\s+(an?\s+|half\s+an?\s+|\d+)\s*" + _UNIT, re.I)
_TO = re.compile(r"\bto\s+(?:remember\s+)?(.+?)[.?!]?\s*$", re.I)
_TIMER = re.compile(r"(?:set\s+)?(?:a\s+)?timer\s+for\s+(an?\s+|half\s+an?\s+|\d+)\s*"
                    + _UNIT, re.I)
_MINUTE_WORDS = ("minute", "minutes", "mins", "min", "m")
_HOUR_WORDS = ("hour", "hours", "hrs", "hr", "h")


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


def _seconds(amount: str, unit: str) -> float | None:
    unit = unit.lower()
    if amount.startswith("half"):
        n = 0.5
    else:
        n = 1.0 if amount in ("a", "an") else float(amount)
    if unit in _HOUR_WORDS:
        return n * 3600.0
    if unit in _MINUTE_WORDS:
        return n * 60.0
    return n                                    # seconds


def _first_match(text: str, pattern: re.Pattern) -> tuple[str, str] | None:
    m = pattern.search(text)
    if not m:
        return None
    return m.group(1), m.group(2)


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
    pair = _first_match(words, _RE_IN)
    if not pair:
        m = _TIMER.search(words)
        if m:
            pair = (m.group(1), m.group(2))
    if not pair:
        return None
    delay = _seconds(pair[0].strip(), pair[1])
    if not delay or delay <= 0 or delay > 7 * 86400:
        return None
    label_m = _TO.search(text)
    if label_m:
        label = label_m.group(1).strip()
    elif re.search(r"timer", words):
        label = "your timer"
    else:
        label = "reminder"
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


def pending() -> list[str]:
    """The not-yet-due, soonest first, with a human gap."""
    now = time.time()
    items = sorted(load(), key=lambda i: i.get("due", 0))
    out = []
    for i in items:
        left = max(0, i.get("due", 0) - now)
        mins = left / 60
        gap = (f"{left:.0f} seconds" if left < 90
               else f"{mins:.0f} minutes" if mins < 120
               else f"{mins / 60:.1f} hours")
        out.append(f"{i['label']} in {gap}")
    return out


def confirm(action: dict) -> str:
    """What to say when the action is taken, before any model is involved."""
    if action["action"] == "cancel":
        n = cancel()
        return (f"Done, {n} reminder(s) cleared." if n
                else "There was nothing to cancel.")
    if action["action"] == "list":
        items = pending()
        if not items:
            return "Nothing pending."
        return "Pending: " + "; ".join(items[:4]) + "."
    if action["action"] == "set":
        add(action["label"], action["delay"])
        mins = action["delay"] / 60
        gap = (f"{action['delay']:.0f} seconds" if action["delay"] < 90
               else f"{mins:.0f} minutes" if mins < 120
               else f"{mins / 60:.1f} hours")
        return f"Done, in {gap}: {action['label']}."
    return ""
