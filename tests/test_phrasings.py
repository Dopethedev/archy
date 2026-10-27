"""Phrases it must understand, and phrases it must leave alone.

Both halves matter. A matcher that only ever fires repaints the screen while
someone is talking about a game; one that only ever refuses drops requests it
was asked to serve. Every phrase here is either something a person would say to
it, or something they would say near it, and several were measured rather than
invented: spoken into this microphone at headset level, "brief me" comes back as
"brief man" or "breathe me", which is why the briefing accepts those, and why
the recogniser is told to expect the project's own words (config.WHISPER_HINT).
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archy import briefing                              # noqa: E402
from archy.assistant import Assistant                   # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


print("-- a theme command repaints the screen --")
FOR_THEME = {
    "stealth": ["switch to stealth mode", "go to stealth mode", "stealth mode",
                "stealth mode please", "stealth mode now", "put it in stealth mode",
                "give me stealth mode", "can you switch to stealth mode",
                "please switch to stealth", "i want stealth mode", "go stealth",
                "turn on stealth mode", "enable stealth mode", "set the theme to stealth",
                "switch theme to stealth"],
    "standard": ["back to standard mode", "switch to standard mode", "standard mode",
                 "back to normal mode", "go back to regular mode", "restore the theme",
                 "reset the theme", "please go back to standard mode"],
    "combat": ["combat mode", "change to combat mode", "make it combat mode",
               "let me have combat mode", "activate combat mode", "battle mode",
               "red alert", "i want combat mode", "change the theme to combat",
               "combat mode please"],
}
for theme, phrases in FOR_THEME.items():
    wrong = [p for p in phrases if Assistant._theme_intent(p) != theme]
    check(f"{len(phrases)} ways to ask for {theme}", not wrong, str(wrong))

print("-- and talking about a theme changes nothing --")
QUIET = [
    "what is the best combat mode in this game",
    "is stealth mode good in skyrim",
    "combat mode in elden ring is great",
    "the combat in that game was intense",
    "stealth games are fun",
    "tell me about stealth games",
    "what is stealth mode",
    "does combat mode work",
    "is normal mode any good",
    "how do i change the theme",
    "which mode is best for combat",
    "is that normal",
    "that is normal",
    "i love stealth",
    "the regular one",
    "normal",
    "stealth",
    "what time is it",
    "open brave",
    "run a security sweep",
]
fired = [(p, Assistant._theme_intent(p)) for p in QUIET if Assistant._theme_intent(p)]
check(f"{len(QUIET)} phrases about themes, not to it", not fired, str(fired))

print("-- the briefing, including how it is actually heard --")
for text in ("brief me", "briefing", "give me a briefing", "good morning",
             "status report", "what's the weather like", "a brief summary",
             # measured: what this microphone makes of "brief me"
             "brief man", "brief main", "brief mane", "breathe me", "breath me"):
    check(f"briefing wanted for {text!r}", briefing.wanted(text))
for text in ("open brave", "stealth mode", "run a security sweep",
             "set a timer for ten minutes", "what do you know about my sister"):
    check(f"no briefing for {text!r}", not briefing.wanted(text))

print("-- a time question stays local, so it costs no weather fetch --")
check("time is light", briefing.wants_light("what time is it"))
check("date is light", briefing.wants_light("what day is it"))
check("weather is not light", not briefing.wants_light("what's the weather like"))
check("a briefing is not light", not briefing.wants_light("brief me"))

print("-- the clock, asked the ways it gets asked --")
# Including the two the project's own demo film types. It asks "what is the
# time", and that went to the model with no facts at all: the one thing a machine
# can answer exactly was the one thing left to be guessed at.
CLOCK = ["what time is it", "what is the time", "what's the time", "whats the time",
         "what time is it now", "what is the time now", "tell me the time",
         "the time please", "got the time", "do you have the time",
         "what day is it", "what day is today", "what is the day today",
         "what is the date", "what's the date", "what is the date today",
         "what's today's date", "tell me the date", "today's date", "todays date",
         "hey archy, what is the time"]
wrong = [t for t in CLOCK if not (briefing.wanted(t) and briefing.wants_light(t))]
check(f"{len(CLOCK)} ways to ask the time or the date", not wrong, str(wrong))

print("-- and a question that only mentions the time is not one --")
MENTIONS = ["what is the time complexity of quicksort",
            "what is the time difference between here and tokyo",
            "what is the time zone of kathmandu",
            "what is the time limit on that",
            "what is the timestamp",
            "what times table is that"]
fired = [t for t in MENTIONS if briefing.wanted(t) or briefing.wants_light(t)]
check(f"{len(MENTIONS)} about code or a place, not about the clock",
      not fired, str(fired))

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}  ({passed} checks)")
sys.exit(1 if failed else 0)
