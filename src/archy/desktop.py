"""The windows on this desktop, and putting one of them in front.

"Focus Firefox" used to be answered by the model, which reached for the tooling it
learned on: the commands for a compositor this machine does not run. So the answer
was a confident sentence about something that did not happen. Window work belongs in
code, the same way reminders and themes do, for the same reason: it is a fact about
this machine, and the model is guessing at it.

The compositor is read from the environment, never assumed. niri, Hyprland and sway
each answer a different command with a different shape, so each has an adapter that
turns its output into the same small record. An environment this does not recognise
gets one honest sentence rather than a guess, because a guess here is a sentence
about doing something that did not happen.

Nothing here closes a window except the one that is already in front, and only when
asked for in so many words. The reply names what it acted on, so a mishearing is
visible immediately.
"""
from __future__ import annotations

import json
import os
import subprocess

from . import config

NIRI = "niri"
HYPRLAND = "hyprland"
SWAY = "sway"
UNKNOWN = "unknown"

# The article and the possessive are how a person says it, not part of a name.
_FILLER = ("the ", "my ", "a ", "an ")
_STRIP = " \t\"'.,!?;:"


def _run(argv: list[str], timeout: float | None = None) -> bytes | None:
    """Run a helper command. None means it did not answer, whatever the reason."""
    try:
        out = subprocess.run(argv, capture_output=True,
                             timeout=config.DESKTOP_TIMEOUT_S if timeout is None else timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def compositor(env: dict | None = None) -> str:
    """Which desktop is running, from the environment it sets for its children."""
    env = os.environ if env is None else env
    if env.get("NIRI_SOCKET"):
        return NIRI
    if env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return HYPRLAND
    if env.get("SWAYSOCK"):
        return SWAY
    return UNKNOWN


# ------------------------------------------------------------------- the records
def _record(id, title, app_id, workspace=None, focused=False, focused_at=0.0) -> dict:
    return {"id": id, "title": str(title or ""), "app_id": str(app_id or ""),
            "workspace": workspace, "focused": bool(focused),
            "focused_at": float(focused_at or 0.0)}


def _from_niri(payload) -> list[dict]:
    records = []
    for window in payload:
        if not isinstance(window, dict) or "id" not in window:
            continue
        # niri stamps the moment each window was last focused, which is what makes
        # "focus the browser" pick the browser you were using rather than any other.
        stamp = window.get("focus_timestamp") or {}
        when = float(stamp.get("secs", 0)) + float(stamp.get("nanos", 0)) / 1e9
        records.append(_record(window.get("id"), window.get("title"), window.get("app_id"),
                               window.get("workspace_id"), window.get("is_focused"), when))
    return records


def _from_hyprland(payload) -> list[dict]:
    records = []
    for window in payload if isinstance(payload, list) else []:
        if not isinstance(window, dict):
            continue
        address = window.get("address")
        if not address:
            continue
        history = window.get("focusHistoryID")
        workspace = (window.get("workspace") or {}).get("id")
        records.append(_record(address, window.get("title"),
                               window.get("class") or window.get("initialClass"), workspace,
                               history == 0, 1e6 - (history if isinstance(history, int) else 1e6)))
    return records


def _sway_stamp(stamp) -> float:
    """sway stamps a window with [seconds, nanoseconds] and older builds with a
    plain number. Both shapes turn up in the same tree, and a list where a number
    was expected is how this crashed the first time it met a real one."""
    if isinstance(stamp, (list, tuple)) and len(stamp) == 2:
        try:
            return float(stamp[0]) + float(stamp[1]) / 1e9
        except (TypeError, ValueError):
            return 0.0
    if isinstance(stamp, (int, float)):
        return float(stamp) / 1e6
    return 0.0


def _walk_sway(node, records: list[dict]) -> None:
    if not isinstance(node, dict):
        return
    props = node.get("window_properties") or {}
    if node.get("pid") or props:
        app_id = (node.get("app_id") or props.get("class") or props.get("instance") or "")
        records.append(_record(node.get("id"), node.get("name"), app_id,
                               (node.get("workspace") or {}).get("num") or node.get("workspace"),
                               node.get("focused"), _sway_stamp(node.get("focus"))))
    for child in list(node.get("nodes") or []) + list(node.get("floating_nodes") or []):
        _walk_sway(child, records)


def _from_sway(payload) -> list[dict]:
    records: list[dict] = []
    _walk_sway(payload, records)
    return records


_ADAPTERS = {
    NIRI: (["niri", "msg", "-j", "windows"], _from_niri),
    HYPRLAND: (["hyprctl", "-j", "clients"], _from_hyprland),
    SWAY: (["swaymsg", "-t", "get_tree", "--raw"], _from_sway),
}


def windows(env: dict | None = None, run=None) -> list[dict]:
    """Every window the compositor knows about, as records. Empty if it will not say."""
    kind = compositor(env)
    adapter = _ADAPTERS.get(kind)
    if adapter is None:
        return []
    argv, parse = adapter
    raw = (run or _run)(argv)
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return []
    records = parse(payload)
    # the newest focused window sorts first, which is what matching breaks ties on
    return sorted(records, key=lambda record: record["focused_at"], reverse=True)


# ---------------------------------------------------------------- choosing one
def _needles(needle: str, aliases: dict | None = None) -> list[str]:
    cleaned = (needle or "").strip(_STRIP).lower()
    for filler in _FILLER:
        if cleaned.startswith(filler):
            cleaned = cleaned[len(filler):]
    if not cleaned:
        return []
    found = [cleaned]
    for spoken, names in (aliases if aliases is not None else config.DESKTOP_ALIASES).items():
        if spoken == cleaned or cleaned in spoken.split():
            found.extend(names)
    return found


def _score(record: dict, needle: str) -> int:
    app_id = record["app_id"].lower()
    title = record["title"].lower()
    tokens = app_id.replace("_", "-").split("-")
    if app_id == needle:
        return 4
    if needle in tokens:
        return 3
    if app_id.startswith(needle) or needle in app_id:
        return 2
    if needle in title:
        return 1
    return 0


def match(records: list[dict], needle: str, aliases: dict | None = None) -> dict | None:
    """The window a spoken name means, or None.

    A name that is exactly an application wins over one that merely appears in a
    title, and among equals the one focused most recently wins, so "the browser"
    means the browser you were last using.
    """
    needles = _needles(needle, aliases)
    if not needles:
        return None
    best, best_score = None, 0
    for record in records:
        score = max((_score(record, one) for one in needles), default=0)
        if score > best_score:
            best, best_score = record, score
    return best


# ------------------------------------------------------------------- commands
def focus_command(kind: str, record: dict) -> list[str] | None:
    if kind == NIRI:
        return ["niri", "msg", "action", "focus-window", "--id", str(record["id"])]
    if kind == HYPRLAND:
        return ["hyprctl", "dispatch", "focuswindow", f"address:{record['id']}"]
    if kind == SWAY:
        return ["swaymsg", f'[con_id={record["id"]}]', "focus"]
    return None


def close_command(kind: str) -> list[str] | None:
    """Closing acts on the focused window: the compositors take no id here, which is
    also why nothing but the window in front is ever closed."""
    if kind == NIRI:
        return ["niri", "msg", "action", "close-window"]
    if kind == HYPRLAND:
        return ["hyprctl", "dispatch", "killactive"]
    if kind == SWAY:
        return ["swaymsg", "kill"]
    return None


def focus(needle: str, env: dict | None = None, run=None, aliases: dict | None = None) -> dict:
    """What happened, in facts, so the caller can say it plainly."""
    kind = compositor(env)
    records = windows(env, run)
    if not records:
        return {"ok": False, "reason": why_not(kind), "windows": []}
    chosen = match(records, needle, aliases)
    if chosen is None:
        return {"ok": False, "reason": "no match", "windows": records, "asked": needle}
    if chosen["focused"]:
        return {"ok": True, "window": chosen, "already": True, "windows": records}
    argv = focus_command(kind, chosen)
    if argv is None:
        return {"ok": False, "reason": why_not(kind), "windows": records}
    if (run or _run)(argv) is None:
        return {"ok": False, "reason": "the compositor refused", "window": chosen,
                "windows": records}
    return {"ok": True, "window": chosen, "already": False, "windows": records}


def close_focused(env: dict | None = None, run=None) -> dict:
    kind = compositor(env)
    argv = close_command(kind)
    if argv is None:
        return {"ok": False, "reason": why_not(kind)}
    focused = next((record for record in windows(env, run) if record["focused"]), None)
    if focused is None:
        return {"ok": False, "reason": "nothing is in front"}
    if (run or _run)(argv) is None:
        return {"ok": False, "reason": "the compositor refused", "window": focused}
    return {"ok": True, "window": focused}


def why_not(kind: str | None = None) -> str:
    kind = kind or compositor()
    if kind == UNKNOWN:
        return ("I cannot see the windows here: this desktop is not one I know how to "
                "ask. I know niri, Hyprland and sway.")
    name = "niri" if kind == NIRI else kind
    return f"I could not get the window list out of {name}."


# -------------------------------------------------------------------- spoken
def _name(record: dict) -> str:
    """What a person would call this window: the application, not the app id.

    "brave-browser" is Brave, "code-oss" is Code, and "org.gnome.nautilus" is
    Nautilus, because a reverse-domain id says the vendor first and the program last.
    """
    app_id = record["app_id"] or "a window"
    if "." in app_id and app_id.split(".")[0] in ("org", "com", "io", "net", "dev"):
        app_id = app_id.rsplit(".", 1)[-1]
    spoken = app_id.replace("_", "-").split("-")[0]
    return spoken[:1].upper() + spoken[1:]


def spoken_list(records: list[dict], limit: int | None = None) -> str:
    if not records:
        return "No windows are open."
    limit = config.DESKTOP_LIST_LIMIT if limit is None else limit
    names: list[str] = []
    for record in records:
        name = _name(record)
        if name not in names:
            names.append(name)
    shown = names[:limit]
    rest = len(names) - len(shown)
    if not shown:
        return "No windows are open."
    spoken = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + f" and {shown[-1]}"
    windows_word = "window" if len(records) == 1 else "windows"
    more = f", and {rest} more" if rest > 0 else ""
    return f"{len(records)} {windows_word} open: {spoken}{more}."


def spoken_focus(result: dict) -> str:
    if result.get("ok"):
        window = result["window"]
        if result.get("already"):
            return f"{_name(window)} is already in front."
        return f"{_name(window)} is in front now."
    if result.get("reason") == "no match":
        asked = (result.get("asked") or "that").strip()
        return (f"I do not see a window called {asked}. " +
                spoken_list(result.get("windows", [])))
    return str(result.get("reason") or "I could not do that.")


def spoken_close(result: dict) -> str:
    if result.get("ok"):
        return f"Closed {_name(result['window'])}."
    if result.get("reason") == "nothing is in front":
        return "There is no window in front to close."
    return str(result.get("reason") or "I could not close that.")


# ------------------------------------------------------------------- intents
def focus_request(text: str) -> str | None:
    """The name a spoken window request is about, or None if this is not one."""
    words = (text or "").strip(_STRIP).lower()
    for trigger in config.DESKTOP_FOCUS_PHRASES:
        if words == trigger:
            continue
        if words.startswith(trigger + " "):
            name = words[len(trigger):].strip(_STRIP)
            if not name or name.isdigit():
                continue
            # "switch to workspace 3", "bring up monitor 2", "go to tab 4" are all
            # something else, and answering them with a window focus would be a
            # sentence about an action that never happened.
            if name.split()[0] in config.DESKTOP_NOT_A_WINDOW:
                continue
            return name
    return None


def list_request(text: str) -> bool:
    words = " ".join((text or "").strip(_STRIP).lower().split())
    return any(phrase in words for phrase in config.DESKTOP_LIST_PHRASES)


def close_request(text: str) -> bool:
    words = " ".join((text or "").strip(_STRIP).lower().split())
    return words in config.DESKTOP_CLOSE_PHRASES
