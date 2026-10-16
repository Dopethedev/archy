"""Entry point.

  python -m jarvis                 talk to it (wake word: "hey jarvis")
  python -m jarvis --open          the same, and open the dashboard
  python -m jarvis --say "hello"   no mic: send one message straight to the brain
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys

from . import config
from .assistant import Assistant
from .hud import Hud

APP_BROWSERS = ["brave", "chromium", "google-chrome-stable", "google-chrome", "firefox"]


def open_dashboard(url: str):
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
    return None


async def one_shot(text: str):
    assistant = Assistant(hud=None)
    await assistant.brain.start()
    assistant.player.start()
    await assistant._process(text=text)
    assistant.shutdown()


def main():
    parser = argparse.ArgumentParser(prog="jarvis", description="a voice assistant")
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
    parser.add_argument("--seconds", type=float, help="stop after this many seconds")
    args = parser.parse_args()

    if args.engine:
        config.TTS_ENGINE = args.engine
    if args.voice:
        config.TTS_VOICE = args.voice
    if args.port:
        config.HUD_PORT = args.port

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
        hud.start()
        print(f"dashboard: {hud.url}")

    assistant = Assistant(hud=hud, greet=args.greet or "Online.")
    if args.open and hud:
        opened = open_dashboard(hud.url)
        print(f"opened dashboard in {opened}" if opened else "no browser found to open the dashboard")

    try:
        asyncio.run(assistant.run())
    except KeyboardInterrupt:
        print("\ninterrupted")
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
