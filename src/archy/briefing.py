"""The briefing: time, date, weather, machine health, the last sweep.

Gathered locally, before the model is asked, so the answer costs one turn and
the facts are real rather than whatever the model remembers. The weather comes
from open-meteo, which needs no key and costs nothing; everything else is read
off this machine.
"""
import json
import re
import time
import urllib.request

from . import config, sysinfo

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
    if any(w in words for w in config.BRIEF_WORDS) or clock_ask(words):
        return True
    # Two words this microphone turns this into, measured rather than guessed:
    # spoken at headset level, "brief me" comes back as "brief main", "brief man"
    # or "breathe me" often enough to matter, and a briefing that silently never
    # happens is worse than one asked for with odd words. A bare "brief" counts
    # too, so "a brief summary" works, which is a reasonable thing to want.
    if re.search(r"\bbrief(ing|ly)?\b", words):
        return True
    return bool(re.search(r"\bbr(?:ief|eath|eathe)\s+(?:me|main|man|may)\b", words))


# The clock and the calendar, asked the ways they get asked. The list in the
# config holds the phrases this microphone has been measured on; this holds the
# shape of the question itself, because the two questions the project's own demo
# film types were neither: "what is the time" and "what's the time" went to the
# model with no facts at all, so the one thing a machine can answer exactly was
# the one thing it was left to guess at.
_CLOCK_ASK = re.compile(
    r"\b(?:"
    r"what(?:'?s| is)? (?:the )?time(?: is it| now)?|what time is it|"
    r"tell me the time|the time please|got the time|do you have the time|"
    r"what(?:'?s| is)? (?:the |today'?s )?date|what day is (?:it|today)|"
    r"what(?:'?s| is)? the day today|tell me the date|today'?s date"
    r")\b")

# "what is the time complexity of this" is a question about code, not about the
# clock, and the model is the right thing to ask. So are the ones that ask about a
# place, which need an offset this machine does not know.
_NOT_A_CLOCK = ("complexity", "difference", "zone", "limit", " in ", " it takes")

# A clock ask with any of these in it is a briefing that happens to start with the
# clock: "what day is it going to rain" is a weather question, and "what is the
# date and how is the machine" wants the machine report as well.
_ALSO_MORE = ("weather", "rain", "forecast", "temperature", "brief", "machine",
              "status", "sweep", "system")


def clock_ask(text: str) -> bool:
    """A time or date question, as opposed to a question containing those words."""
    words = text.lower()
    return bool(_CLOCK_ASK.search(words)) \
        and not any(w in words for w in _NOT_A_CLOCK)


def wants_light(text: str) -> bool:
    """A time or date ask and nothing else: no weather fetch, no machine report."""
    words = text.lower()
    return clock_ask(words) and not any(w in words for w in _ALSO_MORE)


def _weather() -> str:
    """One line of sky. Cached for half an hour; the sky does not move faster."""
    global _weather_cache
    if config.LATITUDE is None or config.LONGITUDE is None:
        return "unavailable, no location is set"
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
    """The machine in one sentence, or an admission that it will not say.

    The readings come from sysinfo, which is what knows whether this is a mac or
    a Linux box. A machine with none of them is told apart from a machine whose
    readings all happen to be zero by the uptime and the load average, which
    every machine that can be asked has.
    """
    load = sysinfo.load_average()
    up = sysinfo.uptime_seconds()
    mem, _ = sysinfo.memory()
    if load is None or up is None:
        return "machine readings unavailable"
    hours, mins = int(up // 3600), int(up % 3600 // 60)
    if mem <= 0.0:                              # a platform that cannot say
        return f"load {load[0]:.1f}, up {hours}h {mins:02d}m"
    return f"load {load[0]:.1f}, memory at {mem:.0f} percent, up {hours}h {mins:02d}m"


def _last_sweep() -> str:
    path = config.STATE_DIR / "last-sweep.txt"
    try:
        text = path.read_text(errors="replace")
        when = time.strftime("%H:%M", time.localtime(path.stat().st_mtime))
    except OSError:
        return "no sweep has run yet on my watch"
    alerts = text.count("alert:")
    if alerts:
        return f"the last sweep, at {when}, raised {alerts} alert(s): look into it"
    return f"the last sweep, at {when}, came back clean"


def clock() -> str:
    """Just the clock and the calendar, for a question that asked for those.

    The same facts block, with one line in it. Asked what the time is, the model
    was handed the machine report and the last sweep too, and read all of it out:
    three sentences of uptime after the time nobody asked about.
    """
    return ("LOCAL FACTS, gathered just now. Do not search the web for these.\n"
            f"Time: {time.strftime('%H:%M on %A, %d %B %Y')}.\n")


def gather() -> str:
    """The facts block, in plain sentences a model can read aloud from."""
    lines = [f"Time: {time.strftime('%H:%M on %A, %d %B %Y')}.",
             f"Weather in {config.CITY or 'your area'}: {_weather()}.",
             f"This machine: {_machine()}.",
             f"Security: {_last_sweep()}."]
    return ("LOCAL FACTS, gathered just now. Do not search the web for these.\n"
            + "\n".join(lines) + "\n")
