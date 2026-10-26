"""What the dashboard shows, and what it refuses to make up.

The panel had cpu, memory, temperature and reply delay, which is the machine and
not the assistant. Nothing said which model was answering, whether it was a relay or
the model on this machine, whether the network was up, whether a picture would leave
the machine when asked, or that an action was sitting there waiting to be approved.

Everything here is read from state that already exists. Nothing probes the network,
starts a model server, or keeps its own copy of anything: a second monitoring
subsystem would be a second set of numbers to disagree with the first.

Two rules this group exists to hold. Unavailable is not zero: a temperature sensor
that cannot be read, a connectivity verdict that has not been reached, a waiting list
that cannot be read are each reported as unknown, because a dashboard that invents a
number is worse than one that admits it has none. And no secret reaches the payload:
what is shown was built for showing.
"""
import asyncio
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import archy.hud as hud_mod  # noqa: E402
import archy.local_model as local_model  # noqa: E402
import archy.status as status_mod  # noqa: E402
from archy import config, net, permissions, privacy  # noqa: E402
from archy.hud import Hud  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    ok = bool(ok)
    passed += ok
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:58s} {detail}")


saved = {name: getattr(config, name, object()) for name in (
    "DASHBOARD_STATUS", "PI_MODELS", "PI_MODELS_OFFLINE", "PI_CHAIN_MODE",
    "PERMISSIONS", "PERMISSION_PENDING", "PRIVACY_SHARE_MEMORY")}

print("-- one snapshot, from state that already exists --")
config.PI_MODELS = ("some-relay/large-model",)
config.PI_MODELS_OFFLINE = ("local/Qwen3.5-4B-Q4_K_M",)
LOCAL_ID = "local/Qwen3.5-4B-Q4_K_M"
config.PI_CHAIN_MODE = "auto"
config.PERMISSIONS = False


class StubBrain:
    def __init__(self, model="some-relay/large-model", chain=None):
        self._model = model
        self.chain = chain if chain is not None else ("some-relay/large-model", "local/small")

    def model_name(self):
        return self._model


snapshot = status_mod.snapshot(brain=StubBrain(), latency=1.4, events=status_mod.Events())
check("it says which model is answering", snapshot["model"]["active"] == "some-relay/large-model")
check("and whether that is a relay or this machine", snapshot["model"]["local"] is False)
check("and what the chain would try next", snapshot["model"]["configured"] == ["some-relay/large-model", "local/small"])
check("a local model is reported as local",
      status_mod.snapshot(brain=StubBrain(LOCAL_ID, chain=(LOCAL_ID,)), latency=None,
                          events=status_mod.Events())["model"]["local"] is True)
check("and a model whose name merely starts with local is not",
      status_mod.snapshot(brain=StubBrain("local/not-configured", chain=("local/not-configured",)),
                          latency=None, events=status_mod.Events())["model"]["local"] is False,
      "the offline chain is the list, not a naming convention")
empty = status_mod.snapshot(brain=StubBrain(chain=()), latency=None, events=status_mod.Events())
check("an empty chain says no model is ready", empty["model"]["ready"] is False)
check("and says why, rather than showing a blank", bool(empty["model"].get("reason")), str(empty["model"].get("reason")))

print("-- the mode, the network, the local server --")
for internet, provider, wanted in ((True, True, net.ONLINE), (False, True, net.OFFLINE),
                                   (True, False, net.DEGRADED)):
    net.net._internet, net.net._provider = internet, provider
    got = status_mod.snapshot(brain=StubBrain(), latency=None, events=status_mod.Events())
    check(f"a {wanted} verdict is reported as {wanted}", got["connectivity"]["state"] == wanted)
net.net._internet, net.net._provider = None, None
unset = status_mod.snapshot(brain=StubBrain(), latency=None, events=status_mod.Events())
check("a verdict that has not been reached says checking, not online",
      unset["connectivity"]["state"] == "checking", str(unset["connectivity"]))
check("and the mode it implies is named", unset["mode"] in ("auto", "online", "offline"),
      str(unset["mode"]))

real_healthy = local_model.healthy
local_model.healthy = lambda *a, **k: (_ for _ in ()).throw(OSError("no socket"))


def boom(*a, **k):
    raise OSError("no socket")


local_model.healthy = boom
broken = status_mod.snapshot(brain=StubBrain(), latency=None, events=status_mod.Events())
check("a local server that cannot be asked is unknown, not stopped",
      broken["local_model"]["running"] is None, str(broken["local_model"]))
local_model.healthy = lambda *a, **k: False
check("one that is not running says so", status_mod.snapshot(
    brain=StubBrain(), latency=None, events=status_mod.Events())["local_model"]["running"] is False)
local_model.healthy = lambda *a, **k: True
check("and one that is, says that", status_mod.snapshot(
    brain=StubBrain(), latency=None, events=status_mod.Events())["local_model"]["running"] is True)
local_model.healthy = real_healthy

print("-- nothing here starts anything --")
real_popen = subprocess.Popen


def forbidden(*a, **k):
    raise AssertionError("a snapshot tried to start a process")


subprocess.Popen = forbidden
real_ensure = local_model.ensure_running
local_model.ensure_running = forbidden
try:
    status_mod.snapshot(brain=StubBrain(), latency=None, events=status_mod.Events())
    untouched = True
except AssertionError as exc:
    untouched = False
    print("   ", exc)
finally:
    subprocess.Popen = real_popen
    local_model.ensure_running = real_ensure
check("a snapshot does not start a model server", untouched)
check("and does not spawn anything else either", untouched)
check("while a real snapshot is happy with a fake local server", isinstance(
    status_mod.snapshot(brain=StubBrain(), latency=None,
                        events=status_mod.Events())["local_model"], dict))

print("-- latency, present or missing --")
check("a measured turn is reported", status_mod.snapshot(
    brain=StubBrain(), latency=1.44, events=status_mod.Events())["latency"]["last_s"] == 1.44)
missing = status_mod.snapshot(brain=StubBrain(), latency=None, events=status_mod.Events())
check("no turn yet is unknown, not zero", missing["latency"]["last_s"] is None)

print("-- the recent events, and their ceiling --")
events = status_mod.Events()
check("a new list is empty", events.recent() == [])
events.add("fallback", "the relay failed, so the local model answered")
events.add("error", "the relay refused the key")
check("what happened is kept in order", [e["kind"] for e in events.recent()] == ["fallback", "error"])
check("with a time on each", all(isinstance(e["ts"], (int, float)) for e in events.recent()))
for index in range(200):
    events.add("error", f"event {index}")
check("and the list does not grow without bound", len(events.recent()) == status_mod.MAX_EVENTS,
      f"{len(events.recent())} kept")
check("the newest are the ones kept", events.recent()[-1]["text"] == "event 199")

secrets = "token=sk-super-secret-value"
events.add("error", f"failed with {secrets}")
payload = status_mod.snapshot(brain=StubBrain(), latency=1.0, events=events)
check("a secret in an event does not reach the dashboard", secrets not in json.dumps(payload))
check("but the failure itself is still visible",
      "failed with" in json.dumps(payload))
check("and the redaction says what it hid", "[redacted]" in json.dumps(payload))

print("-- what may leave the machine, as the switches have it --")
state = status_mod.snapshot(brain=StubBrain(), latency=None, events=status_mod.Events())
check("every privacy switch is reported", set(state["privacy"]) == set(privacy.SWITCHES),
      str(sorted(state["privacy"])))
config.PRIVACY_SHARE_MEMORY = False
state = status_mod.snapshot(brain=StubBrain(), latency=None, events=status_mod.Events())
check("a switch that is off is reported off", state["privacy"]["memory"] is False)
check("and the local model is still not restricted", privacy.shares("memory", LOCAL_ID) is True)
check("a relay is", privacy.shares("memory", "some-relay/large-model") is False)
config.PRIVACY_SHARE_MEMORY = True

print("-- waiting on approval --")
config.PERMISSIONS = False
state = status_mod.snapshot(brain=StubBrain(), latency=None, events=status_mod.Events())
check("with the gate off it says off", state["permissions"]["enforcing"] is False)
check("and does not claim anything is being checked", state["permissions"].get("reason"),
      str(state["permissions"].get("reason")))

config.PERMISSIONS = True
check("with the gate on it says so", status_mod.snapshot(
    brain=StubBrain(), latency=None, events=status_mod.Events())["permissions"]["enforcing"] is True)
config.PERMISSION_PENDING = Path("/nonexistent/pending.json")
check("no waiting list at all is nothing waiting, which is a real zero",
      status_mod.snapshot(brain=StubBrain(), latency=None,
                          events=status_mod.Events())["permissions"]["waiting"] == 0)
unreadable = Path(tempfile.mkdtemp()) / "pending.json"
unreadable.write_text("{not json at all")
config.PERMISSION_PENDING = unreadable
check("a waiting list that cannot be read is unknown, not zero",
      status_mod.snapshot(brain=StubBrain(), latency=None,
                          events=status_mod.Events())["permissions"]["waiting"] is None)
config.PERMISSION_PENDING = saved["PERMISSION_PENDING"]
config.PERMISSIONS = False

print("-- the dashboard event itself --")
published = []
hud = Hud(port=8931)
hud.publish = lambda event: published.append(event)
check("nothing is published when the feature is off",
      (hud.status_source is None) and published == [], "the default is today's dashboard")
config.DASHBOARD_STATUS = True
hud.status_source = lambda: status_mod.snapshot(brain=StubBrain(), latency=1.1,
                                               events=status_mod.Events())
check("turned on, the dashboard is given a source", hud.status_source is not None)

# the loop, with the waiting replaced and the publishing real
class TimedStop:
    """Lets the real loop run its body a fixed number of times, then stops it.

    The body is the code that ships. Only the waiting is replaced, because waiting
    for a real second and a half is what makes a test slow rather than stronger.
    """

    def __init__(self, runs):
        self.left = runs

    def is_set(self):
        return False

    def wait(self, timeout=None):
        self.left -= 1
        return self.left < 0


feed = status_mod.Feed(brain=StubBrain())
feed.note_turn(1.4)
feed.note("fallback", "the relay failed, so the local model answered")
check("the feed carries the last turn's delay", feed.snapshot()["latency"]["last_s"] == 1.4)
check("and what it was told", feed.snapshot()["events"][0]["kind"] == "fallback")
feed.note_turn(None)
check("a turn with no delay is unknown in the feed too", feed.snapshot()["latency"]["last_s"] is None)
feed.note_turn(2.0)
check("and the next turn replaces it rather than averaging", feed.snapshot()["latency"]["last_s"] == 2.0)

config.DASHBOARD_STATUS = False
check("switched off, the feed offers the dashboard nothing", feed.as_source() is None)
config.DASHBOARD_STATUS = True
check("switched on, it offers the panel", callable(feed.as_source()))

hud = Hud(port=8931)
ticks = []
hud.publish = lambda event: ticks.append(event)
hud.status_source = feed.as_source()
hud._stop = TimedStop(5)
hud._stats_loop()
kinds = [t.get("type") for t in ticks]
stats = [t for t in ticks if t.get("type") == "stats"]
statuses = [t for t in ticks if t.get("type") == "status"]
check("the stats event still arrives", len(stats) == 5, str(kinds))
check("and the status event arrives with it", len(statuses) == 2, str(kinds))
check("the first status comes straight away, not five seconds later",
      kinds[0] == "stats" and kinds[1] == "status", str(kinds[:4]))
check("the stats event keeps its keys, so nothing that reads it breaks",
      {"cpu", "mem", "mem_gb", "temp"} <= set(stats[0]), str(sorted(stats[0])))
check("the status carries the model", statuses[0]["model"]["active"] == "some-relay/large-model")
check("and the mode", "mode" in statuses[0])
check("and the privacy switches", "privacy" in statuses[0])
check("and the waiting count", "permissions" in statuses[0])
check("and the delay of the last turn", statuses[0]["latency"]["last_s"] == 2.0)
check("the status is not sent every tick, which would be a second poll",
      len(statuses) < len(stats), f"{len(statuses)} status for {len(stats)} stats")

replaying = Hud(port=8934)
for n in range(5):
    replaying.publish({"type": "status", "model": {"active": f"m{n}"}})
    replaying.publish({"type": "stats", "cpu": n, "mem": n, "mem_gb": n, "temp": n})
check("a fresh page is primed with one status, not a wall of them",
      len([e for e in replaying.replay if e.get("type") == "status"]) == 1,
      str([e.get("type") for e in replaying.replay]))
check("and the latest one is the one kept",
      [e for e in replaying.replay if e.get("type") == "status"][0]["model"]["active"] == "m4")
check("the transcript is not evicted by the status stream", len(replaying.replay) <= 40)

# a source that fails must not take the stats loop down with it
hud2 = Hud(port=8932)
broken = []
hud2.publish = lambda event: broken.append(event)
hud2.status_source = boom
hud2._stop = TimedStop(3)
hud2._stats_loop()
check("a status source that fails does not stop the stats",
      len([t for t in broken if t.get("type") == "stats"]) == 3, str(broken)[:90])
told = [t for t in broken if t.get("type") == "status"]
check("and the panel is told it could not be read, rather than made up",
      bool(told) and "unavailable" in told[0], str(told)[:90])
check("and the loop carries on afterwards",
      [t.get("type") for t in broken] == ["stats", "status", "stats", "stats"],
      str([t.get("type") for t in broken]))

config.DASHBOARD_STATUS = False
check("switched off again, the dashboard is not given one", feed.as_source() is None)

print("-- and the page paints what it is given --")


def painted(payload: dict) -> dict:
    """Run the dashboard's own script in node and read back what it wrote.

    A browser cannot be asked: the page holds an open event stream, so a headless
    screenshot waits for a page that never finishes loading. The script itself is
    plain javascript with no build step, so it runs under a small DOM instead, and
    what comes back is what it really painted.
    """
    try:
        proc = subprocess.run(
            ["node", str(Path(__file__).resolve().parent / "paint_status.mjs"),
             json.dumps(payload)],
            capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        return {"error": "node is not installed, and this group needs it"}
    if proc.returncode != 0:
        return {"error": (proc.stderr.strip().splitlines() or ["node failed"])[-1]}
    return json.loads(proc.stdout)


live = status_mod.Feed(brain=StubBrain())
live.note_turn(1.4)
shown = painted(live.snapshot())
check("the page runs and paints", "error" not in shown, str(shown.get("error", ""))[:90])
values = shown.get("values", {})
check("the panel is revealed once a status arrives", shown.get("hidden") is False)
check("it names the model", values.get("model") == "some-relay/large-model", str(values.get("model")))
check("and says it is a relay, not this machine", values.get("where") == "a relay")
check("it shows the mode", values.get("mode") == "auto")
check("it shows the network verdict", values.get("connectivity") in
      ("checking", "online", "offline", "degraded"), str(values.get("connectivity")))
check("it shows the local server's state", values.get("local_model") in ("up", "down", "unknown"))
check("it shows what may leave the machine", values.get("privacy_online") == "the model only")
check("it shows nothing waiting", values.get("waiting") == "0")
check("and says so when there is nothing to report",
      shown.get("events") == ["nothing to report"], str(shown.get("events")))

unknown = live.snapshot()
unknown["permissions"]["waiting"] = None
unknown["local_model"]["running"] = None
unknown["connectivity"]["state"] = "online"
unknown["connectivity"]["provider"] = False
unknown["privacy"]["online_ai"] = False
for n in range(4):
    unknown["events"].append({"ts": time.time(), "kind": "error", "text": f"failure {n}"})
shown = painted(unknown)
values = shown.get("values", {})
check("a waiting list it cannot read is shown as unknown, never as nothing",
      values.get("waiting") == "unknown", str(values.get("waiting")))
check("a local server it cannot ask is unknown too",
      values.get("local_model") == "unknown", str(values.get("local_model")))
check("a refusing provider is shown, not read as an outage",
      values.get("connectivity") == "online (provider refusing)", str(values.get("connectivity")))
check("online AI switched off is shown as nothing leaving",
      values.get("privacy_online") == "nothing", str(values.get("privacy_online")))
check("only the last few events are shown", len(shown.get("events", [])) == 3, str(shown.get("events")))
check("and the newest are the ones shown", shown.get("events", [""])[-1].endswith("failure 3"),
      str(shown.get("events")))

print("-- the page has somewhere to put it --")
html = (Path(__file__).resolve().parents[1] / "hud" / "index.html").read_text()
script = (Path(__file__).resolve().parents[1] / "hud" / "app.js").read_text()
check("the panel exists", 'id="status-panel"' in html)
check("the page handles the status event", 'msg.type === "status"' in script)
for field in ("model-active", "model-where", "mode", "connectivity", "local-model",
              "privacy-online", "waiting"):
    check(f"and has a place for {field}", f'id="{field}"' in html, field)
check("the panel starts hidden, so a dashboard without the feature is unchanged",
      "status-panel\" hidden" in html or 'id="status-panel" hidden' in html)
check("the page holds no credential of its own",
      "apiKey" not in script and "apiKey" not in html and "sk-" not in script)
check("and the only token in it is the placeholder the server fills in",
      html.count("__ARCHY_TOKEN__") == 1
      and html.count('window.ARCHY_TOKEN = "__ARCHY_TOKEN__"') == 1,
      "one assignment, and its value is the placeholder")
check("while the script only ever reads that value, never defines one",
      "window.ARCHY_TOKEN" in script and "ARCHY_TOKEN =" not in script)
check("it says unavailable rather than zero when the waiting list cannot be read",
      "unknown" in script and "waiting" in script)

print("-- and no secret in the payload either --")


def keys_in(node, found=None):
    found = [] if found is None else found
    if isinstance(node, dict):
        for key, value in node.items():
            found.append(str(key))
            keys_in(value, found)
    elif isinstance(node, list):
        for item in node:
            keys_in(item, found)
    return found


payload_keys = keys_in(payload)
check("nothing in the payload is named like a credential",
      not [k for k in payload_keys if re.search(r"key|token|secret|password", k, re.I)],
      str([k for k in payload_keys if re.search(r"key|token|secret|password", k, re.I)]))
check("and the payload is plain data a page can render",
      all(isinstance(v, (str, int, float, bool, list, dict, type(None)))
          for v in [payload["model"], payload["connectivity"], payload["permissions"],
                    payload["privacy"], payload["latency"]]))

for name, value in saved.items():
    setattr(config, name, value)
print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}  ({passed} checks)")
sys.exit(1 if failed else 0)
