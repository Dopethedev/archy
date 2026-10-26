"""The assistant's own state, gathered in one place for showing.

The dashboard had the machine: cpu, memory, temperature, reply delay. It had nothing
about the assistant, so the questions worth asking were unanswerable from the screen.
Which model is answering, and is it a relay or the one on this disk? Is the network
up, and if it is, is the provider refusing? Would a screenshot leave the machine if
asked? Is something sitting there waiting to be approved?

Every value here is read from state that already exists, and none of it is measured
again. Nothing probes the network, nothing starts a model server, and there is no
second copy of any number: a separate monitor would be a second set of figures to
disagree with the first, which is a worse dashboard than a small one.

Two rules, because a status panel is read as fact:

  Unavailable is not zero. A sensor that cannot be read, a verdict not yet reached,
  a waiting list that will not parse are reported as unknown. A number invented to
  fill the space is worse than a space.
  Nothing secret is in the payload. Event text passes the same redaction the logs get,
  so a token that reached a log line does not become a token on a web page.
"""
from __future__ import annotations

import time

from . import config, local_model, net, permissions, privacy

MAX_EVENTS = 50


class Events:
    """What happened recently, capped.

    Kept in memory on purpose: this is a panel, not an audit trail. The permission
    log is the record, and it is on disk with the decisions in it.
    """

    def __init__(self, limit: int = MAX_EVENTS):
        self.limit = limit
        self._items: list[dict] = []

    def add(self, kind: str, text: str) -> None:
        self._items.append({"ts": time.time(), "kind": str(kind),
                            "text": permissions.redact(str(text))})
        del self._items[:max(0, len(self._items) - self.limit)]

    def recent(self, count: int | None = None) -> list[dict]:
        if count is None:
            return list(self._items)
        return list(self._items[-count:])


def _model(brain) -> dict:
    # An empty chain is a fact about the assistant, not a missing attribute: only a
    # brain that has no chain at all falls back to the configured list.
    chain = getattr(brain, "chain", None)
    configured = list(config.PI_MODELS if chain is None else chain)
    try:
        active = brain.model_name() if brain is not None else None
    except Exception:
        active = None
    ready = bool(active) and bool(configured)
    # One definition of local, from the offline chain, and not from what a model is
    # called: an id that merely looks like a local one is still a relay.
    local = False if not active else not privacy.model_is_remote(active)
    out = {
        "active": active,
        "configured": configured,
        "local": local,
        "ready": ready,
        "chain_mode": config.PI_CHAIN_MODE,
    }
    if not ready:
        out["reason"] = ("no model is configured" if not configured
                         else "the chain has no model selected")
    return out


def _connectivity() -> dict:
    """The verdict as the connectivity object has it, without asking it for one."""
    try:
        reading = net.net.snapshot()
    except Exception as exc:
        return {"state": "unknown", "detail": permissions.redact(str(exc))}
    return {
        "state": reading.get("mode") or "checking",
        "internet": reading.get("internet"),
        "provider": reading.get("provider"),
        "checked": reading.get("checked"),
    }


def _local_model() -> dict:
    """Whether the local server is up. Unknown when it cannot be asked."""
    try:
        running = local_model.healthy()
    except Exception:
        running = None
    return {
        "configured": [model for model in config.PI_MODELS_OFFLINE],
        "running": running,
    }


def _permissions() -> dict:
    state = permissions.state()
    waiting = permissions.waiting_count()
    out = {
        "enabled": state["enabled"],
        "enforcing": state["enforcing"],
        "waiting": waiting,
        "reason": state["reason"],
    }
    if waiting:
        out["pending"] = [{"tool": entry.get("tool"),
                           "summary": permissions.redact(str(entry.get("summary", "")))}
                          for entry in permissions.pending()]
    return out


class Feed:
    """The one object that holds what the panel needs, and the one the dashboard asks.

    The assistant updates it where things happen, so the panel is a reading of
    events rather than a second opinion about them.
    """

    def __init__(self, brain=None):
        self.brain = brain
        self.events = Events()
        self.last_latency: float | None = None

    def note_turn(self, latency: float | None) -> None:
        self.last_latency = float(latency) if latency else None

    def note(self, kind: str, text: str) -> None:
        self.events.add(kind, text)

    def snapshot(self) -> dict:
        return snapshot(brain=self.brain, latency=self.last_latency, events=self.events)

    def as_source(self):
        """What the dashboard should call, or None when the panel is switched off."""
        return self.snapshot if config.DASHBOARD_STATUS else None


def snapshot(brain=None, latency: float | None = None, events: Events | None = None) -> dict:
    """Everything the status panel shows, in one dictionary."""
    return {
        "at": time.time(),
        "model": _model(brain),
        "mode": config.PI_CHAIN_MODE,
        "connectivity": _connectivity(),
        "local_model": _local_model(),
        "privacy": privacy.switches(),
        "permissions": _permissions(),
        "latency": {"last_s": round(float(latency), 2)
                    if isinstance(latency, (int, float)) and latency >= 0 else None},
        "events": events.recent() if events is not None else [],
    }
