"""Get the voice models, in one command.

The install used to end in a table and four hand downloads from two sites, which
is where most people would stop. Everything here is free, and three of the four
come from one release.

    python -m archy.fetch_models           the models it needs to speak
    python -m archy.fetch_models --all     and the piper fallback voice
    python -m archy.fetch_models --check   say what is here and what is not

Each file is downloaded to a `.part` and only renamed into place once the whole
thing is there and the length matches what the server promised, so an interrupted
download cannot leave a half file that looks like a model. After that it is
opened the way the assistant opens it, because a file that is present is not the
same as a file that loads.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config

KOKORO_RELEASE = ("https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
                  "model-files-v1.1")
SILERO_URL = ("https://raw.githubusercontent.com/snakers4/silero-vad/master/"
              "src/silero_vad/data/silero_vad.onnx")
PIPER_VOICE = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/en/"
               "en_GB/alan/medium")
USER_AGENT = "archy-fetch-models/1.0"


class FetchError(Exception):
    """A model could not be fetched, or is not what it claims to be."""


@dataclass(frozen=True)
class Item:
    name: str
    url: str | None          # None means an installed package already has it
    what: str
    package_file: str | None = None     # copied out of an installed package
    optional: bool = False


ITEMS = (
    Item("kokoro-v1.0.onnx", f"{KOKORO_RELEASE}/kokoro-v1.0.onnx", "the voice"),
    Item("voices-v1.0.bin", f"{KOKORO_RELEASE}/voices-v1.0.bin", "the voice's accents"),
    Item("silero_vad.onnx", SILERO_URL, "the voice activity detector",
         package_file="silero_vad.onnx"),
    Item("en_GB-alan-medium.onnx", f"{PIPER_VOICE}/en_GB-alan-medium.onnx",
         "the fallback voice", optional=True),
    Item("en_GB-alan-medium.onnx.json", f"{PIPER_VOICE}/en_GB-alan-medium.onnx.json",
         "the fallback voice's settings", optional=True),
)


def plan(all_models: bool = False) -> list[Item]:
    return [i for i in ITEMS if all_models or not i.optional]


def in_package(name: str) -> Path | None:
    """Some models ship inside a package that is already installed."""
    try:
        import openwakeword
    except ImportError:
        return None
    path = Path(openwakeword.__file__).parent / "resources" / "models" / name
    return path if path.exists() else None


def _human(n: int) -> str:
    return f"{n / 1024 / 1024:.0f} MB" if n > 1024 * 1024 else f"{n / 1024:.0f} kB"


def download(item: Item, target: Path, force: bool = False) -> str:
    """Fetch one file. Returns a line for the log, raises FetchError on failure."""
    if target.exists() and not force:
        return f"already here  {item.name}  ({_human(target.stat().st_size)})"
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    if item.url is None:
        raise FetchError(f"{item.name}: nothing to download and no package has it")
    started = time.monotonic()
    try:
        request = urllib.request.Request(item.url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as resp, open(part, "wb") as out:
            promised = resp.headers.get("Content-Length")
            got = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
        if got == 0:
            raise FetchError(f"{item.name}: the server sent nothing")
        if promised is not None and int(promised) != got:
            raise FetchError(f"{item.name}: got {got} bytes of {promised}")
        part.replace(target)                 # atomic, so no half model exists
    except (urllib.error.URLError, OSError, ValueError) as exc:
        part.unlink(missing_ok=True)
        raise FetchError(f"{item.name}: {exc}") from exc
    except FetchError:
        part.unlink(missing_ok=True)
        raise
    return (f"fetched       {item.name}  ({_human(target.stat().st_size)} "
            f"in {time.monotonic() - started:.0f}s)")


def copy_from_package(item: Item, target: Path, force: bool = False) -> str | None:
    """Use the copy an installed package already has, if there is one."""
    if target.exists() and not force:
        return None
    if not item.package_file:
        return None
    source = in_package(item.package_file)
    if source is None:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return f"copied        {item.name}  ({_human(target.stat().st_size)} from openwakeword)"


def loads_ok(models: Path, item: Item) -> str:
    """Open it the way the assistant does. Empty string means fine."""
    path = models / item.name
    try:
        if item.name.startswith("kokoro"):
            voices = models / "voices-v1.0.bin"
            if not voices.exists():
                return ""                    # checked with its own item
            from kokoro_onnx import Kokoro
            Kokoro(str(path), str(voices))
        elif item.name.endswith(".onnx"):
            import onnxruntime as ort
            ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        elif item.name.endswith(".json"):
            import json
            json.loads(path.read_text(encoding="utf-8"))
        else:
            return ""
    except Exception as exc:                 # a truncated file, a bad download
        return f"{type(exc).__name__}: {exc}"
    return ""


def report(models: Path, items: list[Item]) -> list[str]:
    lines = []
    for item in items:
        path = models / item.name
        if not path.exists():
            lines.append(f"missing       {item.name}  ({item.what})")
        else:
            lines.append(f"here          {item.name}  ({_human(path.stat().st_size)})")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="archy.fetch_models",
                                     description="fetch the voice models")
    parser.add_argument("--all", action="store_true",
                        help="include the optional piper fallback voice")
    parser.add_argument("--check", action="store_true",
                        help="say what is here, fetch nothing")
    parser.add_argument("--force", action="store_true", help="fetch again over what is here")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip opening the files once they are here")
    parser.add_argument("--dir", default=None, help="where to put them (default: models/)")
    opts = parser.parse_args(args)

    models = Path(opts.dir) if opts.dir else config.MODELS
    items = plan(opts.all)

    if opts.check:
        for line in report(models, items):
            print(line)
        missing = [i for i in items if not (models / i.name).exists()]
        if missing:
            print(f"\n{len(missing)} missing: run python -m archy.fetch_models"
                  + (" --all" if any(i.optional for i in missing) else ""))
            return 1
        print("\neverything the assistant needs is here")
        return 0

    print(f"models go in {models}")
    failures = 0
    for item in items:
        # both helpers take the file they are responsible for, not the directory:
        # passing the directory wrote one model to the directory's own path and
        # then reported every other item as already present at that size
        path = models / item.name
        try:
            copied = copy_from_package(item, path, opts.force)
            if copied:
                print(f"  {copied}")
                continue
            print(f"  {download(item, path, opts.force)}")
        except FetchError as exc:
            failures += 1
            print(f"  FAILED        {item.name}: {exc}")
            if item.optional:
                continue
    if failures:
        # a required model missing is not a warning: the assistant would fall
        # back to a worse voice, or refuse to speak, and say nothing about why
        print(f"\n{failures} could not be fetched. Fix the connection and run it again; "
              "nothing half written was left behind.")
        return 1

    if not opts.no_verify:
        print("checking each one opens the way the assistant opens it")
        for item in items:
            if not (models / item.name).exists():
                continue
            complaint = loads_ok(models, item)
            if complaint:
                print(f"  FAILED        {item.name} does not load: {complaint}")
                failures += 1
        if failures:
            print("\nDelete the files above and run it again.")
            return 1

    print("\nready. Whisper fetches its own model on the first answer, about 140 MB, "
          "and lives in the huggingface cache, not here.")
    if not opts.all:
        print("The piper fallback voice is optional: --all fetches it, and it needs "
              "the piper binary (uv sync --extra fallback-voice).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
