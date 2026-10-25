"""The part of the permission system that lives in JARVIS.

The enforcement is in src/jarvis/gate/permission-gate.ts, inside pi, because that
is where a tool call can actually be refused. This module is everything around it:
writing the policy the gate reads, handing pi the flag that loads it, asking the
owner about what was blocked, recording the answer as a single-use approval, and
reading the decisions back without printing anything secret.

The two sides never talk directly. They share four files under .state, which is
deliberate: the gate runs in a different process, on the far side of a pipe, and
a file is the one thing that survives a restart of either of them.

    permission-policy.json     written here at start-up, read by the gate
    permission-pending.json    written by the gate when it blocks something
    permission-approvals.json  written here when the owner approves, consumed by the gate
    permission-log.jsonl       written by the gate, read here

Switched off by default. With PERMISSIONS off there is no gate, no policy file and
no enforcement at all, which is today's behaviour and is stated plainly wherever
the state of the machine is shown, because a security feature that is off must
never be reported as on.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from . import config
from . import privacy


class GateMissing(RuntimeError):
    """The gate is switched on but the file that enforces it is not there."""


# ------------------------------------------------------------------ readiness


def enabled() -> bool:
    return bool(config.PERMISSIONS)


def gate_file() -> Path:
    return Path(config.PERMISSION_GATE)


def ready() -> bool:
    """Whether the enforcement file is where it should be. True when switched off."""
    if not enabled():
        return True
    return gate_file().is_file()


def gate_args() -> list[str]:
    """The flags that load the gate, and nothing when it is off.

    `--no-extensions` stays on so discovery, and with it the MCP handshake cost of
    a helper process per turn, does not come back. An explicit `-e` path is still
    loaded with discovery disabled, which this pi version states in its own help
    text, and which is why the gate can be one file.
    """
    if not ready() or not enabled():
        return []
    return ["-e", str(gate_file())]


def gate_env() -> dict[str, str]:
    if not ready() or not enabled():
        return {}
    return {"JARVIS_PERMISSION_POLICY": str(config.PERMISSION_POLICY)}


# The brain publishes the model it is using here, so the policy written for the gate
# describes the request the gate is going to be judging. A module-level slot rather
# than a parameter because there are three callers and one current answer.
_current_model: tuple = (None,)


def current_model() -> tuple:
    return _current_model


def note_model(model: str | None) -> None:
    """Called by the brain when the chain moves, and followed by write_policy()."""
    global _current_model
    _current_model = (model,)


def policy() -> dict:
    """The policy the gate enforces.

    Roots are the assistant's own directories plus whatever the owner added. The
    home directory is deliberately not one: approving everything a program can
    reach is the same as having no gate, and the point of this is that reading a
    document in Documents is a decision someone made.
    """
    roots = [str(config.ROOT), str(config.STATE_DIR)]
    for extra in config.PERMISSION_ROOTS:
        resolved = str(Path(extra).expanduser().resolve())
        if resolved not in roots:
            roots.append(resolved)
    # Which model is answering is part of the policy, because the gate judges the
    # file tools and it cannot see the brain's chain from inside pi.
    remote = privacy.model_is_remote(current_model()[0]) if current_model() else True
    return {
        "version": 1,
        "enabled": True,
        "privacy_remote": remote,
        "privacy_allow_files": bool(privacy.shares("files", current_model()[0])),
        "roots": roots,
        "readonly_commands": list(config.PERMISSION_READONLY_COMMANDS),
        "approvals": str(config.PERMISSION_APPROVALS),
        "pending": str(config.PERMISSION_PENDING),
        "log": str(config.PERMISSION_LOG),
        "approval_ttl_s": float(config.PERMISSION_APPROVAL_TTL_S),
    }


def write_policy() -> Path:
    """Write the policy where the gate will look for it.

    Raises when the gate is on and cannot be found, rather than writing a policy
    nothing will read: starting the assistant with a promise of enforcement and no
    enforcement is worse than not starting it.
    """
    if enabled() and not ready():
        raise GateMissing(f"the permission gate is not at {gate_file()}")
    target = Path(config.PERMISSION_POLICY)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(policy(), indent=2)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(body)
    os.replace(temporary, target)
    os.chmod(target, 0o600)
    return target


# ------------------------------------------------------------------ approvals


def _read(path: Path) -> dict:
    try:
        parsed = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(body, indent=2))
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def pending() -> list[dict]:
    """What the gate blocked and the owner has not answered yet."""
    entries = _read(Path(config.PERMISSION_PENDING)).get("pending")
    if not isinstance(entries, list):
        return []
    ttl = float(config.PERMISSION_APPROVAL_TTL_S)
    now = time.time()
    return [entry for entry in entries if isinstance(entry, dict)
            and not (isinstance(entry.get("asked"), (int, float)) and now - entry["asked"] > ttl)]


def _drop_pending(entry_id: str) -> bool:
    path = Path(config.PERMISSION_PENDING)
    entries = _read(path).get("pending")
    if not isinstance(entries, list):
        return False
    kept = [entry for entry in entries if not (isinstance(entry, dict)
            and (entry.get("id") == entry_id or entry.get("digest") == entry_id))]
    if len(kept) == len(entries):
        return False
    _write(path, {"pending": kept})
    return True


def approve(entry_id: str) -> str | None:
    """Allow exactly one action, once, and give back its digest.

    The token is keyed by the digest of the action, so approving a command does
    not approve a different command that looks like it, and the gate burns the
    token when it uses it, so a retried turn cannot run the same thing twice.
    Returns None when nothing was waiting under that id, which is the honest
    answer rather than writing an approval for something that was never asked.
    """
    entry = next((item for item in pending()
                  if item.get("id") == entry_id or item.get("digest") == entry_id), None)
    if entry is None:
        return None
    digest = str(entry.get("digest") or "")
    if not digest:
        return None
    path = Path(config.PERMISSION_APPROVALS)
    tokens = _read(path).get("tokens")
    tokens = tokens if isinstance(tokens, list) else []
    # One live token per action: approving twice is the same approval.
    live = [token for token in tokens if isinstance(token, dict) and not token.get("used")]
    if not any(token.get("digest") == digest for token in live):
        live.append({"digest": digest, "granted": time.time(), "id": entry_id, "by": "user"})
    _write(path, {"tokens": live})
    _drop_pending(entry_id)
    return digest


def deny(entry_id: str) -> bool:
    """Take it off the list and grant nothing."""
    return _drop_pending(entry_id)


def summary() -> str:
    """What to say when the owner asks what is waiting."""
    waiting = pending()
    if not waiting:
        return "Nothing is waiting on your approval."
    if len(waiting) == 1:
        return (f"I am waiting on your approval to run: {waiting[0].get('summary', 'something')}. "
                "Say approve to allow it once, or deny to forget it.")
    lines = "; ".join(str(entry.get("summary", "something")) for entry in waiting)
    return (f"{len(waiting)} actions are waiting on your approval: {lines}. "
            "Say approve to allow the most recent one.")


# --------------------------------------------------------------- what may approve

# Approving is a security decision, so the words that trigger it are few and the
# whole utterance has to be one of them. "Yes" is not on the list: it answers
# every question the assistant might ever ask, including the ones it should not be
# able to ask.
_APPROVE_PHRASES = frozenset((
    "approve", "approve it", "approve that", "approve this", "approve action",
    "approve the action", "yes approve", "yes approve it", "allow it", "allow that",
    "i approve", "i approve it",
))
_DENY_PHRASES = frozenset((
    "deny", "deny it", "deny that", "no deny it", "cancel it", "cancel the action",
    "forget it", "forget the action", "do not approve", "don't approve", "never mind",
))


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z']+", text.lower())).strip()


def _is_question(text: str) -> bool:
    return "?" in text


def _matches(text: str, phrases: frozenset[str]) -> bool:
    if _is_question(text):
        return False
    spoken = _words(text)
    if not spoken or len(spoken.split()) > 5:
        return False
    return spoken in phrases


def approve_intent(text: str) -> bool:
    """Whether the owner said approve, and nothing more.

    This is deliberately not conditioned on something waiting. If it were, "approve"
    with an empty list would fall through to the model, and a model asked to approve
    something answers yes, which is the one answer that must never come from a model.
    `approve()` is what refuses when the list is empty.
    """
    return _matches(text, _APPROVE_PHRASES)


def deny_intent(text: str) -> bool:
    return _matches(text, _DENY_PHRASES)


# ----------------------------------------------------------------- the record


_SECRET = re.compile(
    r"(?i)((?:api[_-]?key|token|secret|password|passwd|authorization)\s*[=:]\s*|\bbearer\s+)\S+")


def redact(text: str) -> str:
    return _SECRET.sub(lambda match: f"{match.group(1)}[redacted]", str(text))


def recent_log(count: int = 20, path: Path | None = None) -> list[dict]:
    """The last decisions, with anything secret taken back out on the way through."""
    source = Path(path or config.PERMISSION_LOG)
    try:
        lines = source.read_text().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines[-count:]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append({key: redact(value) if isinstance(value, str) else value
                            for key, value in entry.items()})
    return entries


def state() -> dict:
    """Everything the dashboard and the log command need, in one place."""
    enforcing = enabled() and ready()
    return {
        "enabled": enabled(),
        "enforcing": enforcing,
        "gate": str(gate_file()),
        "waiting": len(pending()),
        "reason": None if enforcing else (
            "the permission gate is switched off, so tool calls are not checked"
            if not enabled() else "the permission gate file is missing, so nothing is enforced"),
    }


# ------------------------------------------------------------------- command line
# The safe half of approving: a terminal, a named action, one word. Voice approval
# exists too, but a mechanism that needs no microphone is worth having when the
# thing being approved is destructive.

def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("status", "state"):
        current = state()
        print("permission gate: " + ("on" if current["enforcing"] else "OFF (nothing is enforced)"))
        if current["reason"]:
            print(f"  {current['reason']}")
        print(f"  gate:      {current['gate']}")
        print(f"  policy:    {config.PERMISSION_POLICY}")
        print(f"  roots:     {', '.join(policy()['roots'])}")
        print(f"  waiting:   {current['waiting']}")
        return 0

    command = args[0]
    if command in ("list", "ls", "waiting"):
        waiting = pending()
        if not waiting:
            print("nothing is waiting on your approval")
            return 0
        for entry in waiting:
            age = int(time.time() - float(entry.get("asked") or 0))
            print(f"{entry.get('id')}  {entry.get('tool')}  {entry.get('summary')}  ({age}s ago)")
        return 0
    if command == "approve" and len(args) >= 2:
        digest = approve(args[1])
        if digest is None:
            print(f"nothing is waiting under {args[1]}")
            return 1
        print(f"approved once: {digest[:8]}")
        return 0
    if command == "deny" and len(args) >= 2:
        print("denied" if deny(args[1]) else f"nothing is waiting under {args[1]}")
        return 0 if deny(args[1]) or True else 1
    if command == "log":
        count = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20
        for entry in recent_log(count):
            print(f"{entry.get('ts', '')}  {entry.get('decision', ''):8s} {entry.get('tool', '')}"
                  f"  {entry.get('summary', '')}  {entry.get('reason', '')}".strip())
        return 0
    if command == "policy":
        print(json.dumps(policy(), indent=2))
        return 0
    if command == "clear":
        _write(Path(config.PERMISSION_PENDING), {"pending": []})
        print("cleared what was waiting; any approval already written still stands")
        return 0
    print("usage: permissions [status|list|approve <id>|deny <id>|log [n]|policy|clear]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
