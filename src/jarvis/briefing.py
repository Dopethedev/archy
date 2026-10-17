"""The briefing: time, date, weather, machine health, the last sweep.

Gathered locally, before the model is asked, so the answer costs one turn and
the facts are real rather than whatever the model remembers. The weather comes
from open-meteo, which needs no key and costs nothing; everything else is read
off this machine.
"""
import json
import time
import urllib.request

from . import config

_weather_cache: tuple[float, str] = (0.0, "")

# open-meteo's WMO weather codes, in speech
WMO = (
    (0, "clear sky"), (1, "mostly clear"), (2, "partly cloudy"), (3, "overcast"),
    (45, "foggy"), (48, "freezing fog"),
    (51, "light drizzle"), (53, "drizzle"), (55, "heavy drizzle"),
    (56, "freezing drizzle"), (57, "heavy freezing drizzle"),
    (61, "light rain"), (63, "rain"), (65, "heavy rain"),
    (66, "freezing rain"), (67, "heavy freezing rain"),
    (71, "light snow"), (73, "snow"), (75, "heavy snow"), (77, "snow grains"),
    (80, "light showers"), (81, "showers"), (82, "violent showers"),
    (85, "snow showers"), (86, "heavy snow showers"),
    (95, "a thunderstorm"), (96, "a thunderstorm with hail"),
    (99, "a severe thunderstorm with hail"),
)


def wanted(text: str) -> bool:
    words = text.lower()
    return any(w in words for w in config.BRIEF_WORDS)


def _weather() -> str:
    """One line of sky. Cached for half an hour; the sky does not move faster."""
    global _weather_cache
    now = time.monotonic()
    if _weather_cache[0] and now - _weather_cache[0] < config.WEATHER_CACHE_S:
        return _weather_cache[1]
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={config.LATITUDE}&longitude={config.LONGITUDE}"
           "&current=temperature_2m,apparent_temperature,weather_code"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
           "&timezone=auto")
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        cur = data["current"]
        day = data["daily"]
        sky = next((say for code, say in WMO if code == cur["weather_code"]),
                   "unusual weather")
        line = (f"{cur['temperature_2m']:.0f} degrees, feels like "
                f"{cur['apparent_temperature']:.0f}, {sky}, today's range "
                f"{day['temperature_2m_min'][0]:.0f} to "
                f"{day['temperature_2m_max'][0]:.0f}, rain chance "
                f"{day['precipitation_probability_max'][0]} percent")
    except Exception as exc:
        line = f"unavailable ({exc.__class__.__name__})"
    _weather_cache = (now, line)
    return line


def _machine() -> str:
    try:
        load1, _, _ = os_loadavg()
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = float(rest.strip().split()[0])
        mem = 100.0 * (1 - info.get("MemAvailable", 1) / info.get("MemTotal", 1))
        with open("/proc/uptime") as fh:
            up = float(fh.read().split()[0])
        hours, mins = int(up // 3600), int(up % 3600 // 60)
        return f"load {load1:.1f}, memory at {mem:.0f} percent, up {hours}h {mins:02d}m"
    except Exception:
        return "machine readings unavailable"


def os_loadavg():
    with open("/proc/loadavg") as fh:
        return tuple(float(x) for x in fh.read().split()[:3])


def _last_sweep() -> str:
    try:
        text = (config.STATE_DIR / "last-sweep.txt").read_text(errors="replace")
    except OSError:
        return "no sweep has run yet on my watch"
    stamp = text.splitlines()[0] if text else ""
    alerts = text.count("alert:")
    if alerts:
        return f"the last sweep, {stamp}, raised {alerts} alert(s): look into it"
    return f"the last sweep, {stamp}, came back clean"


def gather() -> str:
    """The facts block, in plain sentences a model can read aloud from."""
    return (f"LOCAL FACTS, gathered just now. Do not search the web for these.\n"
            f"Time: {time.strftime('%H:%M on %A, %d %B %Y')}.\n"
            f"Weather in Kathmandu: {_weather()}.\n"
            f"This machine: {_machine()}.\n"
            f"Security: {_last_sweep()}.\n")
