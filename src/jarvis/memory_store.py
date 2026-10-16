"""Durable facts.

The session survives a restart, but a session is still just this conversation.
Anything that should outlive it, a codeword, a preference, how someone likes
to be addressed, belongs in a store that is read at startup and written on
request. JSON on disk, no database, because the whole point is that it stays
readable and editable by hand.

Usage from the shell:

    python -m jarvis.memory_store list
    python -m jarvis.memory_store set codeword "purple lantern"
    python -m jarvis.memory_store get codeword
    python -m jarvis.memory_store forget codeword
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from . import config

MAX_KEY = 64
MAX_VALUE = 500


class Memory:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config.MEMORY_FILE
        self.facts: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------ disk
    def _load(self):
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text())
                if isinstance(raw, dict):
                    self.facts = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except Exception as exc:
            print(f"[memory] could not read {self.path}: {exc}", file=sys.stderr)
            self.facts = {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.facts, indent=2, sort_keys=True))
        tmp.replace(self.path)              # atomic, so a crash cannot truncate it

    # ------------------------------------------------------------------- api
    def set(self, key: str, value: str) -> None:
        key = str(key).strip()[:MAX_KEY]
        value = str(value).strip()[:MAX_VALUE]
        if not key:
            raise ValueError("a fact needs a name")
        self.facts[key] = {"value": value, "updated": int(time.time())}
        self._save()

    def get(self, key: str) -> str | None:
        entry = self.facts.get(str(key).strip())
        return entry.get("value") if entry else None

    def forget(self, key: str) -> bool:
        if str(key).strip() in self.facts:
            del self.facts[str(key).strip()]
            self._save()
            return True
        return False

    def summary(self, limit: int = 40) -> str:
        """One readable line per fact, for putting in a prompt."""
        if not self.facts:
            return ""
        items = sorted(self.facts.items())[:limit]
        return "; ".join(f"{k} = {v.get('value', '')}" for k, v in items)

    def line_count(self) -> int:
        return len(self.facts)


# ---------------------------------------------------------------------- cli
def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mem = Memory()
    if not args or args[0] in ("list", "ls", "all"):
        if not mem.facts:
            print("no facts stored")
            return 0
        for key, entry in sorted(mem.facts.items()):
            when = time.strftime("%Y-%m-%d", time.localtime(entry.get("updated", 0)))
            print(f"{key}: {entry.get('value', '')}   ({when})")
        return 0

    command = args[0]
    if command == "set" and len(args) >= 3:
        mem.set(args[1], " ".join(args[2:]))
        print(f"remembered {args[1]}")
        return 0
    if command == "get" and len(args) >= 2:
        value = mem.get(args[1])
        print(value if value is not None else f"nothing stored for {args[1]}")
        return 0 if value is not None else 1
    if command in ("forget", "delete", "rm") and len(args) >= 2:
        print(f"forgot {args[1]}" if mem.forget(args[1]) else f"nothing stored for {args[1]}")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
