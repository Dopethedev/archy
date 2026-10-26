"""Window work, against the shapes real compositors actually print.

The JSON in here was captured from the three compositors, not written by hand, and
niri's was captured from this machine (niri 26.04) with `niri msg -j windows` while
two Brave windows were open. A hand-written fixture would agree with the parser and
prove nothing.

What matters here is the choosing. "Focus the browser" with two browser windows open
has to land on the one you were using, "brave" has to mean the window whose app id is
brave-browser without matching a title that merely mentions it, and a desktop this
code does not know has to say so rather than guess at a command.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import archy.desktop as desktop  # noqa: E402
from archy import config  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    ok = bool(ok)
    passed += ok
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:58s} {detail}")


# captured on this machine: two Brave windows, one focused, one floating
NIRI = json.dumps([
    {"id": 8, "title": "MEGA - Brave", "app_id": "brave-browser", "pid": 18820,
     "workspace_id": 1, "is_focused": False, "is_floating": False, "is_urgent": False,
     "layout": {"pos_in_scrolling_layout": [3, 1], "tile_size": [1920.0, 1046.0],
                "window_size": [1920, 1046], "tile_pos_in_workspace_view": None,
                "window_offset_in_tile": [0.0, 0.0]},
     "focus_timestamp": {"secs": 2340, "nanos": 880857889}},
    {"id": 7, "title": "Watch Dhurandhar: The Revenge 2026 full HD - Brave",
     "app_id": "brave-browser", "pid": 18820, "workspace_id": 1, "is_focused": True,
     "is_floating": False, "is_urgent": False,
     "layout": {"pos_in_scrolling_layout": [2, 1], "tile_size": [1920.0, 1046.0],
                "window_size": [1920, 1046], "tile_pos_in_workspace_view": None,
                "window_offset_in_tile": [0.0, 0.0]},
     "focus_timestamp": {"secs": 2341, "nanos": 0}},
    {"id": 12, "title": "hello.py - code-oss", "app_id": "code-oss", "pid": 18880,
     "workspace_id": 2, "is_focused": False, "is_floating": True, "is_urgent": False,
     "layout": {"pos_in_scrolling_layout": None, "tile_size": [800.0, 600.0],
                "window_size": [800, 600], "tile_pos_in_workspace_view": None,
                "window_offset_in_tile": [0.0, 0.0]},
     "focus_timestamp": {"secs": 2300, "nanos": 0}},
])

# hyprctl -j clients, trimmed to the fields this reads
HYPR = json.dumps([
    {"address": "0x55d1f0a2b3c0", "title": "MEGA - Brave", "class": "brave-browser",
     "workspace": {"id": 1, "name": "1"}, "focusHistoryID": 3},
    {"address": "0x55d1f0a2b410", "title": "notes.md - code-oss", "class": "code-oss",
     "workspace": {"id": 2, "name": "2"}, "focusHistoryID": 0},
])

# swaymsg -t get_tree, trimmed the same way
SWAY = json.dumps({
    "id": 1, "name": "root", "nodes": [
        {"id": 4, "name": "1", "type": "workspace", "nodes": [
            {"id": 9, "name": "MEGA - Brave", "app_id": "brave-browser", "pid": 18820,
             "focused": False, "focus": [1774512040, 880857889],
             "window_properties": {"class": "Brave-browser"}},
            {"id": 10, "name": "terminal", "app_id": "ghostty", "pid": 18900,
             "focused": True, "focus": [1774512042, 0],
             "window_properties": {"instance": "ghostty"}},
        ]},
    ], "floating_nodes": [],
})

print("-- which desktop is this --")
check("niri is recognised from its socket",
      desktop.compositor({"NIRI_SOCKET": "/run/user/1000/niri.sock"}) == desktop.NIRI)
check("Hyprland from its signature",
      desktop.compositor({"HYPRLAND_INSTANCE_SIGNATURE": "abc"}) == desktop.HYPRLAND)
check("sway from its socket",
      desktop.compositor({"SWAYSOCK": "/run/user/1000/sway.sock"}) == desktop.SWAY)
check("and nothing recognised is not guessed at",
      desktop.compositor({}) == desktop.UNKNOWN)
# The real thing, which is a different check on a runner than on a desktop: the
# runner has no compositor of these three, and what matters there is that asking
# gets one honest sentence instead of an exception.
real = desktop.windows()
if real:
    check("reading this machine's windows gives records with what matching needs",
          all({"id", "title", "app_id", "focused"} <= set(record) for record in real),
          f"{len(real)} windows")
    check("and one of them is in front", any(record["focused"] for record in real))
else:
    check("a machine with no compositor of these three reads as empty",
          real == [] and desktop.compositor() == desktop.UNKNOWN, desktop.compositor())
    check("and asking there is a sentence, not an exception",
          desktop.focus("brave")["ok"] is False
          and "niri" in desktop.spoken_focus(desktop.focus("brave")),
          desktop.spoken_focus(desktop.focus("brave")))
check("an unknown desktop gets one honest sentence",
      "niri" in desktop.why_not(desktop.UNKNOWN) and desktop.close_command(desktop.UNKNOWN) is None,
      desktop.why_not(desktop.UNKNOWN))

print("-- reading each one --")
calls = []


def niri_run(argv):
    calls.append(argv)
    return NIRI.encode()


records = desktop.windows({"NIRI_SOCKET": "x"}, run=niri_run)
check("niri's window list is read", len(records) == 3, str(len(records)))
check("with the application name", records[0]["app_id"] in ("brave-browser", "code-oss"))
check("and which one is in front",
      [record["id"] for record in records if record["focused"]] == [7])
check("and when each was last focused, newest first",
      records[0]["focused_at"] > records[-1]["focused_at"], str(records[0]["focused_at"]))
check("the command it ran is niri's", calls[0] == ["niri", "msg", "-j", "windows"], str(calls[0]))

records = desktop.windows({"HYPRLAND_INSTANCE_SIGNATURE": "x"}, run=lambda argv: HYPR.encode())
check("Hyprland's list is read", len(records) == 2)
check("its focused window is the history id of zero",
      [record["app_id"] for record in records if record["focused"]] == ["code-oss"])

records = desktop.windows({"SWAYSOCK": "x"}, run=lambda argv: SWAY.encode())
check("sway's tree is walked, not just read at the top", len(records) == 2, str(len(records)))
check("and its focused window is found too",
      [record["app_id"] for record in records if record["focused"]] == ["ghostty"])

print("-- a compositor that will not answer --")
check("no output means no windows, not a crash",
      desktop.windows({"NIRI_SOCKET": "x"}, run=lambda argv: None) == [])
check("and neither does nonsense",
      desktop.windows({"NIRI_SOCKET": "x"}, run=lambda argv: b"not json at all") == [])
check("nor an empty list", desktop.windows({"NIRI_SOCKET": "x"}, run=lambda argv: b"[]") == [])
check("a list of things that are not windows is skipped, not counted",
      desktop.windows({"NIRI_SOCKET": "x"},
                      run=lambda argv: json.dumps([{"nope": 1}, "text"]).encode()) == [])

print("-- what a spoken name means --")
windows = desktop.windows({"NIRI_SOCKET": "x"}, run=niri_run)
check("an exact application name wins",
      desktop.match(windows, "code-oss")["id"] == 12)
check("a word inside the app id matches, brave-browser by brave",
      desktop.match(windows, "brave")["app_id"] == "brave-browser")
check("and by the second half, browser",
      desktop.match(windows, "browser")["app_id"] == "brave-browser")
check("the article is not part of the name",
      desktop._needles("the browser")[0] == "browser", str(desktop._needles("the browser")))
check("what people call a program is mapped to what the window calls itself",
      "code-oss" in config.DESKTOP_ALIASES["editor"] and
      config.DESKTOP_ALIASES["browser"] and desktop.match(windows, "the editor")["id"] == 12)
check("an alias reaches the window too",
      desktop.match(windows, "my browser")["app_id"] == "brave-browser")
two_braves = [r for r in windows if r["app_id"] == "brave-browser"]
check("with two windows of the same application, the last used one wins",
      desktop.match(windows, "brave")["id"] == 7, str(sorted(r["id"] for r in two_braves)))
check("a title is a weaker match than an application name",
      desktop.match(windows, "dhurandhar")["id"] == 7)
trap = [desktop._record(1, "brave-browser as seen in a video", "mpv", 1, False, 9.0),
        desktop._record(2, "MEGA - Brave", "brave-browser", 1, False, 1.0)]
check("a title that mentions another application does not steal the match",
      desktop.match(trap, "brave")["id"] == 2,
      "the name in the video title is not the application")
check("nothing matching is nothing, not the first window",
      desktop.match(windows, "spotify") is None)
check("and an empty name matches nothing", desktop.match(windows, "  ") is None)
check("an alias list is configurable per call",
      desktop.match(windows, "music", aliases={"music": ("code-oss",)})["id"] == 12)

print("-- focusing --")
calls.clear()


def recording_run(argv):
    calls.append(argv)
    return NIRI.encode() if "windows" in argv else b""


result = desktop.focus("mega", {"NIRI_SOCKET": "x"}, run=recording_run)
check("focusing asks the compositor for that window",
      calls[-1] == ["niri", "msg", "action", "focus-window", "--id", "8"], str(calls[-1]))
check("and reports what it did, with the window it acted on",
      result["ok"] and result["window"]["id"] == 8 and result["already"] is False, str(result))
check("a window behind another is focused, and one in front is only reported",
      desktop.focus("mega", {"NIRI_SOCKET": "x"}, run=recording_run)["already"] is False and
      desktop.focus("dhurandhar", {"NIRI_SOCKET": "x"}, run=recording_run)["already"] is True)
calls.clear()
result = desktop.focus("dhurandhar", {"NIRI_SOCKET": "x"}, run=recording_run)
check("a window already in front is reported, not re-focused",
      result["ok"] and result["already"] is True, str(result))
check("and no focus command was run for it at all",
      all("action" not in argv for argv in calls), str(calls))
check("and it says so out loud",
      desktop.spoken_focus(result) == "Brave is already in front.",
      desktop.spoken_focus(result))
missing = desktop.focus("spotify", {"NIRI_SOCKET": "x"}, run=recording_run)
spoken_missing = desktop.spoken_focus(missing)
check("focusing something that is not open says what is open",
      "spotify" in spoken_missing and "open:" in spoken_missing, spoken_missing)
check("and it never claims to have focused it",
      "is in front now" not in spoken_missing and "already in front" not in spoken_missing,
      spoken_missing)

calls.clear()
check("Hyprland focuses by address",
      desktop.focus_command(desktop.HYPRLAND, {"id": "0x55d1"}) ==
      ["hyprctl", "dispatch", "focuswindow", "address:0x55d1"])
check("sway focuses by con id",
      desktop.focus_command(desktop.SWAY, {"id": 9}) == ["swaymsg", "[con_id=9]", "focus"])
check("and an unknown desktop has no command at all",
      desktop.focus_command(desktop.UNKNOWN, {"id": 1}) is None)


def refusing_run(argv):
    return NIRI.encode() if "windows" in argv else None


refused = desktop.focus("code", {"NIRI_SOCKET": "x"}, run=refusing_run)
check("a compositor that refuses is reported as refusing, not as done",
      refused["ok"] is False and "refused" in refused["reason"], str(refused))
check("and an unknown desktop is refused before anything is run", desktop.focus(
    "brave", {}, run=niri_run)["ok"] is False)

print("-- closing --")
check("closing takes no id, because it acts on the window in front",
      desktop.close_command(desktop.NIRI) == ["niri", "msg", "action", "close-window"])
check("so closing names what it closed",
      desktop.spoken_close({"ok": True, "window": {"app_id": "brave-browser"}}) == "Closed Brave.")
check("and says when there is nothing in front",
      desktop.spoken_close({"ok": False, "reason": "nothing is in front"}) ==
      "There is no window in front to close.")
check("closing with nothing in front does not run a command", desktop.close_focused(
    {"NIRI_SOCKET": "x"}, run=lambda argv: json.dumps([]).encode())["ok"] is False)

print("-- what is asked for, and what is not --")
for text, wanted in (("focus the browser", "the browser"), ("switch to code", "code"),
                     ("bring up discord", "discord"), ("jump to steam", "steam"),
                     ("put in front terminal", "terminal")):
    check(f"it takes the name out of {text!r}", desktop.focus_request(text) == wanted,
          str(desktop.focus_request(text)))
check("it stays out of what is an application launch",
      desktop.focus_request("open brave") is None)
check("and out of a screenshot question, which another intent owns",
      desktop.focus_request("show me this error") is None and
      desktop.focus_request("what's on my screen") is None)
check("and out of cancelling a reminder",
      not desktop.close_request("close this") and desktop.close_request("close this window"))
check("and out of a reminder",
      desktop.focus_request("remind me to focus in twenty minutes") is None,
      str(desktop.focus_request("remind me to focus in twenty minutes")))
check("and out of a workspace move, which the model can do",
      desktop.focus_request("switch to workspace 3") is None)
check("the list questions are recognised",
      all(desktop.list_request(text) for text in
          ("what windows are open", "what's open", "list windows", "what do I have open")))
check("and a question that is not one is not",
      not desktop.list_request("open the browser") and not desktop.list_request("focus brave"))
check("closing is recognised only in so many words",
      desktop.close_request("close this window") and desktop.close_request("close the window")
      and not desktop.close_request("close brave")
      and not desktop.close_request("close the browser tab"))

print("-- saying it --")
spoken = desktop.spoken_list(windows)
check("the spoken list names the applications", "Brave" in spoken and "Code" in spoken, spoken)
check("counts the windows, not the applications", spoken.startswith("3 windows open"), spoken)
check("and it does not read out window titles",
      "Dhurandhar" not in spoken and "MEGA" not in spoken, spoken)
check("an empty desktop says so",
      desktop.spoken_list([]) == "No windows are open.", desktop.spoken_list([]))
many = [{"id": n, "app_id": f"app{n}", "title": "", "focused": False, "focused_at": 0.0}
        for n in range(9)]
check("a long list is cut short, because it is spoken",
      desktop.spoken_list(many).count(",") <= 6 and "more" in desktop.spoken_list(many),
      desktop.spoken_list(many))
check("a reverse-domain id is spoken as the program, not the vendor",
      desktop.spoken_list([desktop._record(1, "Files", "org.gnome.nautilus")]) ==
      "1 window open: Nautilus.", desktop.spoken_list([desktop._record(1, "Files", "org.gnome.nautilus")]))
check("the focused reply names the application",
      desktop.spoken_focus({"ok": True, "window": {"app_id": "brave-browser"}}) ==
      "Brave is in front now.")
check("an unhelpful failure is still a sentence",
      desktop.spoken_focus({"ok": False, "reason": ""}) == "I could not do that.")

print(f"\n{'ALL PASS' if not failed else str(failed) + ' FAILED'}  ({passed} checks)")
sys.exit(1 if failed else 0)
