"""Entry point.

  python -m archy                 talk to it (wake phrase: "hey archy")
  python -m archy --open          the same, and open the dashboard
  python -m archy --say "hello"   no mic: send one message straight to the brain
  python -m archy --mic-test      live microphone check
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from . import config
from . import net
from .assistant import Assistant
from .hud import Hud

APP_BROWSERS = ["brave", "chromium", "google-chrome-stable", "google-chrome", "firefox"]

# On macOS a browser is a bundle in /Applications rather than a program on PATH,
# so the same list is looked for as bundles, and the executable inside one is
# started directly: it takes --app and --new-window exactly as it does on Linux,
# and it hands the window to the copy that is already running. Safari is not in
# the list because it has no app mode; the default-browser fallback below opens
# whatever is set, which on a Mac is Safari unless the owner changed it.
MAC_BROWSERS = (
    ("Brave Browser", "Contents/MacOS/Brave Browser", False),
    ("Google Chrome", "Contents/MacOS/Google Chrome", False),
    ("Chromium", "Contents/MacOS/Chromium", False),
    ("Microsoft Edge", "Contents/MacOS/Microsoft Edge", False),
    ("Firefox", "Contents/MacOS/firefox", True),
)
MAC_APP_DIRS = (Path("/Applications"), Path.home() / "Applications",
                Path("/System/Applications"))


def _mac_browser() -> tuple[str, Path, bool] | None:
    """The first browser bundle installed here, or None."""
    for name, inner, firefox in MAC_BROWSERS:
        for base in MAC_APP_DIRS:
            exe = base / f"{name}.app" / inner
            if exe.exists():
                return name, exe, firefox
    return None


def open_dashboard(url: str):
    if sys.platform == "darwin":
        found = _mac_browser()
        if found:
            name, exe, firefox = found
            args = [str(exe), "--new-window", url] if firefox else \
                   [str(exe), f"--app={url}", "--window-size=900,680"]
            try:
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return name
            except Exception:
                pass
    for name in APP_BROWSERS:
        path = shutil.which(name)
        if not path:
            continue
        args = [path, f"--app={url}", "--window-size=900,680"]
        if name == "firefox":
            args = [path, "--new-window", url]
        try:
            subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return name
        except Exception:
            continue
    # None of the usual browsers, or a platform where they are not named this
    # way: hand it to the operating system, which on Windows and macOS means the
    # default browser and on Linux means xdg-open.
    try:
        import webbrowser
        if webbrowser.open(url):
            return "the default browser"
    except Exception:
        pass
    return None


async def run_limited(assistant, seconds: float | None):
    """Run the assistant, and stop it after this long if asked.

    `--seconds` was accepted, documented as "stop after this many seconds", and
    then ignored on every run that was not the microphone check: the flag did
    nothing at all outside it. What it is for is a bounded smoke test, so it now
    bounds the run.
    """
    if seconds is None:
        await assistant.run()
        return
    try:
        await asyncio.wait_for(assistant.run(), timeout=seconds)
    except asyncio.TimeoutError:
        print(f"stopped after {seconds:g}s")


async def one_shot(text: str):
    assistant = Assistant(hud=None)
    await assistant.start_brain()
    # guarded, so a machine with no output device still gets its answer printed
    # instead of a traceback out of the constructor: the rest of the assistant is
    # built to carry on without a part that will not start
    try:
        assistant.player.start()
    except Exception as exc:
        print(f"no speaker: {type(exc).__name__}: {exc}", flush=True)
    await assistant._process(text=text)
    assistant.shutdown()


def main():
    parser = argparse.ArgumentParser(prog="archy", description="a voice assistant")
    parser.add_argument("--say", metavar="TEXT", help="one typed message, no microphone, then exit")
    parser.add_argument("--open", action="store_true", help="open the dashboard window")
    parser.add_argument("--no-hud", action="store_true", help="no dashboard, console only")
    parser.add_argument("--greet", default="", help="say this when starting up")
    parser.add_argument("--engine", choices=("kokoro", "piper"), help="override the speech engine")
    parser.add_argument("--voice", help="override the voice name")
    parser.add_argument("--port", type=int, help="dashboard port")
    parser.add_argument("--mic-test", action="store_true",
                        help="live microphone check: watch the level while you talk")
    parser.add_argument("--input", help="input device name substring, for example Fifine")
    parser.add_argument("--seconds", type=float,
                        help="stop after this many seconds (the run, or the mic check)")
    args = parser.parse_args()

    if args.engine:
        config.TTS_ENGINE = args.engine
    if args.voice:
        config.TTS_VOICE = args.voice
    if args.port:
        config.HUD_PORT = args.port
    if args.input:
        config.INPUT_DEVICE = args.input      # applies to the real run, not just the check

    if args.say:
        asyncio.run(one_shot(args.say))
        return

    if args.mic_test:
        from .mictest import run as mic_run
        try:
            asyncio.run(mic_run(seconds=args.seconds, device=args.input))
        except KeyboardInterrupt:
            pass
        return

    hud = None if args.no_hud else Hud()
    if hud:
        # A dashboard is a convenience, and the port it wants may already be in
        # use by another copy of this program. Failing to bind it used to end the
        # run before the microphone was opened, so the port was the whole
        # assistant's problem instead of the dashboard's.
        try:
            hud.start()
            print(f"dashboard: {hud.url}")
        except Exception as exc:
            print(f"no dashboard: {type(exc).__name__}: {exc}", flush=True)
            hud = None

    # The greeting says what is true, not what is usual: this used to announce
    # "Online." without checking anything, which is the first thing heard and a
    # lie the moment the network is unplugged.
    assistant = Assistant(hud=hud, greet=args.greet or net.startup_greeting())
    if args.open and hud:
        opened = open_dashboard(hud.url)
        print(f"opened dashboard in {opened}" if opened else "no browser found to open the dashboard")

    try:
        asyncio.run(run_limited(assistant, args.seconds))
    except KeyboardInterrupt:
        print("\ninterrupted")
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
