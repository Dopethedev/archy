"""Whether there is internet, and whether the model provider is answering.

Two questions, and the assistant needs them apart. With no internet the honest
answer is the local model, because waiting on a relay that cannot be reached
costs the user a silence deadline per question. When the provider refuses a model
the connection is obviously fine, and the answer is the next model in the online
chain, which the assistant already knows how to do.

Both arrive as one BrainError string today, which is why nothing can tell them
apart. The order of the patterns below is the part that matters. Asked for a
model it does not have, a relay answers

    404: {"code":"","type":"invalid_request_error","message":"The requested
    resource was not found"}

which is a missing model, not an outage, while a machine with no internet
reports ENOTFOUND. Check "not found" before "enotfound" and both read as the
same thing, so the model patterns are tried first and the DNS spelling keeps its
own name.

The verdict is cached and refreshed out of band: probing before every turn would
spend the user's latency to answer a question the last failure already answered.
No conversation text is ever sent to test anything, and nothing here blocks the
voice loop.
"""

from __future__ import annotations

import socket
import threading
import time

from . import config

# ------------------------------------------------------------------ vocabulary
ONLINE = "online"
OFFLINE = "offline"
DEGRADED = "degraded"           # internet is there, the provider is refusing
CHECKING = "checking"           # nothing known yet

CONNECTIVITY = "connectivity"
AUTH = "auth"
MODEL = "model"
SILENCE = "silence"
UNKNOWN = "unknown"

# Node's fetch, which is what pi uses underneath, reports a dead connection with
# these. A real outage has not been staged on this machine to confirm them all
# (pulling the network out from under a working machine is not a test), so the
# list is the standard set, and `last_error` keeps the raw text of whatever
# really arrived, so a real outage can extend it from evidence rather than a guess.
_CONNECTIVITY = (
    "enotfound", "eai_again", "econnrefused", "econnreset", "etimedout",
    "ehostunreach", "enetunreach", "esockettimedout", "fetch failed",
    "getaddrinfo", "socket hang up", "no route to host", "network is unreachable",
    "network error", "dns error", "offline", "connection refused",
    "connection reset", "connection error", "connect timeout",
    "request timed out", "und_err",
)
_AUTH = (
    "401", "403", "unauthorized", "unauthenticated", "invalid api key",
    "invalid_api_key", "api key", "forbidden", "authentication", "no credentials",
)
# Checked before connectivity, on purpose. See the module docstring.
_MODEL = (
    "404", "not found", "invalid_request_error", "unknown model",
    "custom model id", "model_not_found", "does not exist", "no such model",
)


def classify(message: str) -> str:
    """What kind of failure one BrainError text describes.

    Silence is its own class: a relay that says nothing and a tool run that takes
    a minute look identical from here, so it must not be counted as an outage.
    Anything unrecognised stays UNKNOWN, which keeps the old behaviour rather than
    inventing one.
    """
    text = (message or "").lower()
    if not text:
        return UNKNOWN
    if "said nothing for" in text:
        return SILENCE
    if any(marker in text for marker in _AUTH):
        return AUTH
    if any(marker in text for marker in _MODEL):
        return MODEL
    if any(marker in text for marker in _CONNECTIVITY):
        return CONNECTIVITY
    return UNKNOWN


# ---------------------------------------------------------------------- probec
def _tcp_probe() -> bool:
    """Can anything out there be reached at all? One success is enough.

    A TCP connect to a few well-known addresses, not a DNS lookup and not a web
    request: DNS answers out of a cache, and a captive portal answers a web
    request with a login page, so neither proves the network is really there.
    The targets are tried in order and the first one that connects wins, so a
    single blocked address is not read as an outage.
    """
    for host, port in config.NET_PROBE_TARGETS:
        try:
            with socket.create_connection((host, port),
                                          timeout=config.NET_PROBE_TIMEOUT_S):
                return True
        except OSError:
            continue
    return False


def mode_of(internet: bool | None, provider: bool | None) -> str:
    """The verdict in one word, from the two facts behind it.

    Pure on purpose, so a caller already holding the lock can use it without taking
    the lock again. A method that locks cannot be called from inside another method
    that locks, and the mistake is invisible until something calls it.
    """
    if internet is None:
        return CHECKING
    if not internet:
        return OFFLINE
    return DEGRADED if provider is False else ONLINE


class Connectivity:
    """What is known about the network: cached, refreshed in the background.

    A probe is a hint, not a verdict. Everything here is written so that a turn
    that really happened outranks it: a turn that answered means the internet is
    up whatever a probe says, and a turn that could not connect means it is down.
    When nothing is known, the answer is the optimistic one, which is the
    behaviour from before this module existed.
    """

    def __init__(self, probe=None):
        self._probe = probe or _tcp_probe
        self._lock = threading.Lock()
        self._internet: bool | None = None
        self._provider: bool | None = None
        self._checked = 0.0            # when a probe last finished
        self._started = 0.0            # when a probe last started
        self._running = False
        self.last_error = ""           # the raw text, kept for evidence
        self.last_kind = ""

    # ------------------------------------------------------------- what is known
    def internet(self) -> bool | None:
        with self._lock:
            return self._internet

    def provider(self) -> bool | None:
        with self._lock:
            return self._provider

    def likely_online(self) -> bool:
        """Should the online chain be tried? Unknown counts as yes.

        The cost of being wrong is asymmetric: guessing offline when the internet
        works sends the question to a small local model for no reason, while
        guessing online when it is down costs one failed turn before the failure
        itself proves otherwise.
        """
        with self._lock:
            return self._internet is not False

    def mode(self) -> str:
        with self._lock:
            return mode_of(self._internet, self._provider)

    def snapshot(self) -> dict:
        """For the dashboard and the log: no probes, no waiting."""
        with self._lock:
            # Read the fields once, then judge them with the pure function the mode
            # uses. The first version of this called self.mode() from inside this
            # lock, and mode() takes the same lock, so a reader waited for itself
            # and never returned. Nothing called it until the status panel did,
            # which is why the dashboard is where it surfaced.
            internet, provider = self._internet, self._provider
            return {
                "mode": mode_of(internet, provider),
                "internet": internet,
                "provider": provider,
                "checked": round(self._checked, 1),
                "running": self._running,
                "kind": self.last_kind,
            }

    # ---------------------------------------------------------------- refreshing
    def refresh(self, force: bool = False) -> None:
        """Start a probe in the background when the verdict is old. Returns at once.

        The minimum interval holds even for a forced refresh: a probe is a
        network request, and a machine that is flapping would otherwise be
        probed on every turn.
        """
        now = time.monotonic()
        with self._lock:
            if self._running:
                return
            if self._internet is not None and now - self._started < config.NET_CHECK_MIN_INTERVAL_S:
                return
            if not force and self._internet is not None and now - self._checked < config.NET_CHECK_TTL_S:
                return
            self._running = True
            self._started = now
        threading.Thread(target=self._probe_now, daemon=True).start()

    def _probe_now(self) -> None:
        try:
            up = bool(self._probe())
        except Exception as exc:                    # a probe that raises proves nothing
            print(f"[net] probe failed: {type(exc).__name__}: {exc}", flush=True)
            up = False
        with self._lock:
            self._internet = up
            self._checked = time.monotonic()
            self._running = False

    def wait(self, seconds: float) -> bool | None:
        """Hold for a verdict, at most this long. Used once, at start-up."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            with self._lock:
                if self._internet is not None and not self._running:
                    return self._internet
            time.sleep(0.02)
        return self.internet()

    # ------------------------------------------------------------------ evidence
    def note_success(self) -> None:
        """A turn answered. That is proof of the network and of the provider."""
        with self._lock:
            self._internet = True
            self._provider = True
            self._checked = time.monotonic()
            self.last_kind = ""

    def note_failure(self, message: str) -> str:
        """Record what a failed turn said, and classify it.

        Only a connectivity failure flips the internet verdict downwards: an auth
        or model failure means the provider answered, and reading that as an
        outage would point the chain at a local model over a bad key. It flips the
        verdict upwards instead, because a 401 that came back from a relay is
        packet-for-packet proof that the network is up, whatever a probe said
        before it.
        """
        kind = classify(message)
        with self._lock:
            self.last_kind = kind
            self.last_error = (message or "")[:400]
            if kind == CONNECTIVITY:
                self._internet = False
                self._provider = False
                self._checked = time.monotonic()
            elif kind in (AUTH, MODEL):
                self._internet = True
                self._provider = False
                self._checked = time.monotonic()
        return kind


# The one the assistant uses. Tests build their own with a probe they control.
net = Connectivity()


def startup_greeting(link: Connectivity | None = None,
                     wait: float | None = None) -> str:
    """What to say when it boots, and only what is true.

    This said "Online." every time, without checking anything, which is a lie the
    moment the network is unplugged and the first thing the user hears. The
    verdict usually lands in milliseconds; this waits a short bounded time for it
    and says something neutral if it never arrives.
    """
    link = link or net
    link.refresh()
    link.wait(config.NET_GREETING_WAIT_S if wait is None else wait)
    # Offline-only mode answers from the local model whatever the network is
    # doing, so saying "Online." would be true about the router and a lie about
    # the assistant. The mode is what the user hears.
    if config.PI_CHAIN_MODE == "offline" and config.PI_MODELS_OFFLINE:
        return "Offline. Running on the local model."
    verdict = link.internet()
    if verdict is True:
        return "Online."
    if verdict is False:
        if config.PI_MODELS_OFFLINE:
            return "Offline. Running on the local model."
        return "Offline. I cannot reach the model without internet."
    return "Ready."
