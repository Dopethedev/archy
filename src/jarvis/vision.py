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

from . import config


class ScreenError(Exception):
    """The screen could not be captured."""


def _run(argv: list[str], stdin: bytes | None = None, timeout: float = 10.0) -> bytes:
    proc = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)
    if proc.returncode != 0:
        raise ScreenError(f"{argv[0]} failed: {proc.stderr.decode(errors='replace')[:120]}")
    return proc.stdout


def capture() -> bytes:
    """Return a JPEG of the whole screen. Raises ScreenError on failure."""
    if shutil.which("grim") is None or shutil.which("ffmpeg") is None:
        raise ScreenError("grim or ffmpeg is not installed")
    png = _run(["grim", "-"])
    if not png:
        raise ScreenError("grim produced no image")
    # -f mjpeg with image2pipe keeps ffmpeg from wrapping the frames in a
    # container; the output is a bare jpeg
    return _run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0",
                 "-q:v", "85", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                stdin=png)


def as_image(jpeg: bytes) -> dict:
    """Wrap the bytes the way pi's rpc prompt wants them."""
    return {"type": "image", "data": base64.b64encode(jpeg).decode(),
            "mimeType": "image/jpeg"}


def wanted(text: str) -> bool:
    """Does this request need to see? Deliberately eager: a false yes costs one
    screenshot, a false no means the assistant answers blind."""
    words = text.lower()
    return any(w in words for w in config.SCREEN_WORDS)
