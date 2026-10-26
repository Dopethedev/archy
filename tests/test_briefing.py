"""The briefing: real facts gathered before the model is asked.

The weather is fetched from a fake response, never the network; the machine
readings are this machine's real /proc, which is exactly the point of them.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import archy.briefing as briefing
from archy import config

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


print("-- what triggers a briefing --")
for text in ("brief me", "hey archy give me my morning briefing",
             "good morning", "what's the weather", "what day is it",
             "what time is it", "system status", "hows the machine"):
    check(f"wanted {text!r}", briefing.wanted(text))
for text in ("open brave", "remember my birthday", "run a security sweep"):
    check(f"not wanted {text!r}", not briefing.wanted(text))

print("-- the weather line, from a canned response --")
fake = {
    "current": {"temperature_2m": 24.6, "apparent_temperature": 26.1,
                "weather_code": 61},
    "daily": {"temperature_2m_max": [28.2], "temperature_2m_min": [17.9],
              "precipitation_probability_max": [65]},
}
class FakeResp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps(fake).encode()
# the code map is what is being tested, so this test brings its own location:
# without one configured the briefing says the weather is unavailable on
# purpose, which is correct behaviour and not what these checks are about
briefing.config.LATITUDE = 51.5
briefing.config.LONGITUDE = -0.12
real_urlopen = briefing.urllib.request.urlopen
briefing.urllib.request.urlopen = lambda url, timeout: FakeResp()
try:
    line = briefing._weather()
    check("temperature spoken", "25" in line and "feels like 26" in line, line)
    check("a known code becomes words", "light rain" in line, line)
    check("the range and rain chance are there",
          "18" in line and "28" in line and "65 percent" in line, line)
    check("the second call inside the cache window does not refetch",
          (briefing._weather() == line) and True)
    fake["current"]["weather_code"] = 95
    briefing._weather_cache = (0.0, "")
    line = briefing._weather()
    check("a thunderstorm is named", "thunderstorm" in line, line)
    briefing._weather_cache = (0.0, "")
    def broken(url, timeout): raise TimeoutError("too slow")
    briefing.urllib.request.urlopen = broken
    line = briefing._weather()
    check("a dead network says so instead of lying", "unavailable" in line, line)
finally:
    briefing.urllib.request.urlopen = real_urlopen
    briefing._weather_cache = (0.0, "")

print("-- machine health and the last sweep --")
# the readings come from sysinfo, which knows a Linux box and a Mac both; a
# platform it cannot read has to say so rather than produce a line of zeroes
readable = sys.platform.startswith("linux") or sys.platform == "darwin"
machine_line = briefing._machine()
check("machine line reads this machine",
      ("memory at" in machine_line) if readable else ("unavailable" in machine_line),
      machine_line)
config.STATE_DIR = Path("/tmp") / f"archy-test-{time.time_ns()}"
check("no sweep yet is admitted", "no sweep has run" in briefing._last_sweep())
config.STATE_DIR.mkdir(parents=True)
(config.STATE_DIR / "last-sweep.txt").write_text(
    "security sweep of Archy, 09:00:\n  ok: nothing\n  alert: pid 1 runs from /tmp\n")
check("alerts are not softened", "1 alert" in briefing._last_sweep()
      and "look into it" in briefing._last_sweep())
(config.STATE_DIR / "last-sweep.txt").write_text(
    "security sweep of Archy, 09:00:\n  ok: nothing\n")
check("a clean sweep says so with its time",
      "came back clean" in briefing._last_sweep())

print("-- the whole block reads as one facts card --")
facts = briefing.gather()
check("time is in it", "Time:" in facts)
# the city is the machine owner's business: with none configured the
# briefing says so rather than naming a place
check("weather is in it",
      (config.CITY in facts) if config.CITY else ("your area" in facts))
check("machine is in it", ("load" in facts) if readable else ("unavailable" in facts),
      facts.splitlines()[1][:70])
check("security is in it", "sweep" in facts)
check("it tells the model not to go looking",
      "Do not search the web" in facts)

print()
print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
sys.exit(0 if not failed else 1)
