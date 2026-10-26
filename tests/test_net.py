"""Telling "no internet" apart from "the provider refused me".

Both arrive as one BrainError string, so the classifier is the whole point of the
module: read them as the same thing and a bad API key sends every question to a
small local model, or a missing model name makes the assistant announce that the
internet is down while it is plainly up.

The probe here is injected, so this never touches the network. The relay strings
are the real ones: the 404 is what pi 1.4.0 printed when asked for a model that
does not exist, captured before the classifier was written.
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis import config, net  # noqa: E402
from jarvis.net import CHECKING, DEGRADED, OFFLINE, ONLINE, Connectivity  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:58s} {detail}")


print("-- what each failure actually is --")
RELAY_404 = ('404: {"code":"","type":"invalid_request_error",'
             '"message":"The requested resource was not found"}')
cases = (
    (RELAY_404, net.MODEL, "the relay has no such model"),
    ("Warning: Model \"nope\" not found for provider \"wandb\".", net.MODEL, "pi's own warning"),
    ("getaddrinfo ENOTFOUND relay.example", net.CONNECTIVITY, "DNS is gone"),
    ("fetch failed", net.CONNECTIVITY, "node's connection failure"),
    ("connect ECONNREFUSED 127.0.0.1:8080", net.CONNECTIVITY, "nothing listening"),
    ("request timed out", net.CONNECTIVITY, "blackholed"),
    ("401 Unauthorized", net.AUTH, "bad key"),
    ("403 Forbidden: invalid api key", net.AUTH, "refused key"),
    ("pi said nothing for 25s", net.SILENCE, "a silent relay is not proof of an outage"),
    ("pi process ended unexpectedly", net.UNKNOWN, "a dead process is not an outage either"),
    ("the model failed", net.UNKNOWN, "unrecognised keeps the old behaviour"),
    ("", net.UNKNOWN, "nothing to go on"),
)
for text, expected, why in cases:
    got = net.classify(text)
    check(f"{expected:13s} <- {why}", got == expected, f"got {got}")

print("-- the ordering trap --")
check("ENOTFOUND is not read as a missing model",
      net.classify("getaddrinfo ENOTFOUND api.example.com") == net.CONNECTIVITY)
check("and a real 404 is not read as an outage",
      net.classify(RELAY_404) == net.MODEL,
      "the model patterns are checked first")

print("-- nothing known yet --")
link = Connectivity(probe=lambda: True)
check("mode is checking", link.mode() == net.CHECKING, link.mode())
check("internet is unknown", link.internet() is None)
check("and the online chain is still tried", link.likely_online() is True,
      "unknown must not behave like offline")

print("-- a turn that answered is proof --")
link.note_success()
check("mode is online", link.mode() == net.ONLINE, link.mode())
check("internet yes", link.internet() is True)
check("provider yes", link.provider() is True)

print("-- a turn that could not connect is proof of the other kind --")
link = Connectivity(probe=lambda: True)
link.note_success()
kind = link.note_failure("getaddrinfo ENOTFOUND relay.example")
check("classified as connectivity", kind == net.CONNECTIVITY, kind)
check("mode flips to offline", link.mode() == net.OFFLINE, link.mode())
check("the local chain is preferred", link.likely_online() is False)
check("the raw text is kept for evidence",
      "ENOTFOUND" in link.last_error, link.last_error[:40])

print("-- a refused key must not look like an outage --")
link = Connectivity(probe=lambda: True)
link.note_success()
link.note_failure("401 Unauthorized: invalid api key")
check("internet stays up", link.internet() is True, str(link.internet()))
check("the provider is marked down", link.provider() is False)
check("and the mode says degraded, not offline", link.mode() == net.DEGRADED, link.mode())
check("so the online chain is still the one to use", link.likely_online() is True)

print("-- a missing model must not look like an outage either --")
link = Connectivity(probe=lambda: True)
link.note_success()
link.note_failure(RELAY_404)
check("internet stays up", link.internet() is True, str(link.internet()))
check("online chain still tried", link.likely_online() is True)

print("-- a probe runs at most once per verdict, not once per question --")
calls = []
link = Connectivity(probe=lambda: calls.append(1) or True)
link.refresh()
link.wait(2.0)
check("one probe happened", len(calls) == 1, str(len(calls)))
link.refresh()
time.sleep(0.05)
check("asking again inside the TTL does not probe again", len(calls) == 1, str(len(calls)))
link.refresh(force=True)
time.sleep(0.05)
check("not even a forced refresh inside the minimum interval",
      len(calls) == 1, str(len(calls)))
old_ttl, old_min = config.NET_CHECK_TTL_S, config.NET_CHECK_MIN_INTERVAL_S
config.NET_CHECK_TTL_S = config.NET_CHECK_MIN_INTERVAL_S = 0.0
link.refresh(force=True)
link.wait(2.0)
check("past both intervals it probes again", len(calls) == 2, str(len(calls)))
config.NET_CHECK_TTL_S, config.NET_CHECK_MIN_INTERVAL_S = old_ttl, old_min

print("-- a probe says which way it went --")
link = Connectivity(probe=lambda: False)
link.refresh()
check("no route means offline", link.wait(2.0) is False)
check("and that flips the chain", link.likely_online() is False)


def exploding():
    raise OSError("socket blew up")


link = Connectivity(probe=exploding)
link.refresh()
check("a probe that raises is not proof of internet", link.wait(2.0) is False)
check("and the assistant is still alive", link.mode() == net.OFFLINE, link.mode())

print("-- the greeting, which used to lie --")
# Pinned, because config.local.json is a documented override: an owner who set
# offline-only would otherwise be asserting against a different greeting.
saved_mode, saved_chain = config.PI_CHAIN_MODE, config.PI_MODELS_OFFLINE
config.PI_CHAIN_MODE, config.PI_MODELS_OFFLINE = "auto", ()
link = Connectivity(probe=lambda: True)
check("online says so", net.startup_greeting(link, wait=1.0) == "Online.",
      net.startup_greeting(link, wait=1.0))
offline = Connectivity(probe=lambda: False)
text = net.startup_greeting(offline, wait=1.0)
check("offline does not claim to be online",
      "Online." not in text and text.startswith("Offline"), text)
# with a local model the honest sentence names the thing that will answer, and
# without one it names the thing that cannot be reached. Either way it says what
# is true, which is the point of the function.
if config.PI_MODELS_OFFLINE:
    check("and it names the local model it has", "local model" in text, text)
else:
    check("and it says why it cannot think", "internet" in text, text)

saved = config.PI_MODELS_OFFLINE
config.PI_MODELS_OFFLINE = ("llama/local-model",)
offline = Connectivity(probe=lambda: False)
text = net.startup_greeting(offline, wait=1.0)
check("a local model changes the sentence, not the honesty",
      "local model" in text and "Online." not in text, text)
config.PI_MODELS_OFFLINE = saved

release = threading.Event()
hanging = Connectivity(probe=lambda: release.wait(5) or True)
text = net.startup_greeting(hanging, wait=0.05)
check("no verdict at all says something neutral",
      text == "Ready.", text)
release.set()
time.sleep(0.05)
config.PI_CHAIN_MODE, config.PI_MODELS_OFFLINE = saved_mode, saved_chain

print("-- the feature is off by default --")
# config.local.json is read at import, so this asserts the shape the code works
# with rather than the value an install ships: empty means the chain is exactly
# what it always was, which test_local_chain covers by setting it directly.
check("the local chain is a sequence of model ids",
      isinstance(config.PI_MODELS_OFFLINE, (tuple, list)),
      str(type(config.PI_MODELS_OFFLINE).__name__))
check("and the mode is one of the three it understands",
      config.PI_CHAIN_MODE in ("auto", "online", "offline"), config.PI_CHAIN_MODE)
check("probe targets are addresses, not names",
      all(isinstance(t[0], str) and t[0][0].isdigit() for t in config.NET_PROBE_TARGETS),
      str(config.NET_PROBE_TARGETS))
check("and there is more than one of them",
      len(config.NET_PROBE_TARGETS) > 1, str(len(config.NET_PROBE_TARGETS)))

print("-- reading the verdict does not wait for itself --")


def read_within(obj, call, timeout=2.0):
    """Run a read in a thread and say whether it came back at all.

    A self-deadlock is not an error and not a slow call, it is a stop, and a test
    that waits for it looks the same as a test that is slow. So: a thread and a
    deadline, and the verdict is whether it returned.
    """
    box = {}
    def run():
        try:
            box["value"] = call(obj)
        except Exception as exc:          # an error is a failure, not a hang
            box["error"] = f"{type(exc).__name__}: {exc}"
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    return (not worker.is_alive()), box


returned, box = read_within(Connectivity(probe=lambda: True), lambda obj: obj.snapshot())
check("a fresh reader gets a snapshot back", returned, box.get("error", ""))
check("and the snapshot agrees with the verdict it describes",
      box.get("value", {}).get("mode") == CHECKING, str(box.get("value")))
returned, box = read_within(net.net, lambda obj: obj.snapshot())
check("the reader the dashboard uses gets one back too", returned, box.get("error", ""))
check("and reading it twice in a row still returns",
      read_within(net.net, lambda obj: obj.snapshot())[0])
check("and leaves the lock released", net.net._lock.locked() is False)

for internet, provider, wanted in ((None, None, CHECKING), (False, True, OFFLINE),
                                   (True, False, DEGRADED), (True, True, ONLINE)):
    link = Connectivity(probe=lambda: True)
    link._internet, link._provider = internet, provider
    returned, box = read_within(link, lambda obj: obj.snapshot())
    got = box.get("value", {})
    check(f"a {wanted} verdict is read as {wanted}, and read at all",
          returned and got.get("mode") == wanted and got.get("mode") == link.mode(),
          f"returned={returned} mode={got.get('mode')}")

link = Connectivity(probe=lambda: False)
link.note_failure("getaddrinfo ENOTFOUND example.invalid")
returned, box = read_within(link, lambda obj: obj.snapshot())
check("a failed turn shows as offline in the snapshot",
      returned and box.get("value", {}).get("mode") == OFFLINE,
      f"returned={returned} {box.get('value', {})}")
check("with the raw text kept, for evidence",
      "ENOTFOUND" in link.last_error, link.last_error)
returned, box = read_within(net.net, lambda obj: (obj.mode(), obj.likely_online()))
check("and the other readers on the same object answer as well", returned, box.get("error", ""))

print()
print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
sys.exit(0 if not failed else 1)
