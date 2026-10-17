"""Looking at the screen.

A screenshot is taken with grim (the Wayland compositor's own tool), piped
straight through ffmpeg to a full-resolution JPEG and handed to the model as an
image. Nothing is written to disk: the bytes exist only in memory.

Full resolution matters. A downscaled 1280 px JPEG misread the small text on the
dashboard in testing; the full frame reads it correctly. The JPEG keeps the
payload at about a tenth of the PNG's size, which the relay is happier with.
"""
import base64
import shutil
import subprocess
import time

from . import config


class ScreenError(Exception):
    """The screen could not be captured."""


def _run(argv: list[str], stdin: bytes | None = None, timeout: float = 10.0) -> bytes:
    proc = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)
    if proc.returncode != 0:
        raise ScreenError(f"{argv[0]} failed: {proc.stderr.decode(errors='replace')[:120]}")
    return proc.stdout


def capture(attempts: int = 3) -> bytes:
    """Return a JPEG of the whole screen. Raises ScreenError on failure.

    Retried, because the compositor's screencopy fails on its own about one time
    in eight here: measured over 25 calls, 22 worked and 3 came back with
    "failed to copy output" in a fraction of a second, with a fullscreen video
    playing and nothing else in common. Each try is cheap, and retrying takes the
    rate to nearly certain from the caller's point of view, which matters because
    the alternative is answering a question about the screen while blind.
    """
    if shutil.which("grim") is None or shutil.which("ffmpeg") is None:
        raise ScreenError("grim or ffmpeg is not installed")
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            png = _run(["grim", "-"])
            if not png:
                raise ScreenError("grim produced no image")
            # -f mjpeg with image2pipe keeps ffmpeg from wrapping the frames in a
            # container; the output is a bare jpeg
            return _run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0",
                         "-q:v", "85", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                        stdin=png)
        except ScreenError as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(0.15)
    raise ScreenError(f"{last} (after {attempts} attempts)")


def as_image(jpeg: bytes) -> dict:
    """Wrap the bytes the way pi's rpc prompt wants them."""
    return {"type": "image", "data": base64.b64encode(jpeg).decode(),
            "mimeType": "image/jpeg"}


def wanted(text: str) -> bool:
    """Does this request need to see? Deliberately eager: a false yes costs one
    screenshot, a false no means the assistant answers blind."""
    words = text.lower()
    return any(w in words for w in config.SCREEN_WORDS)
