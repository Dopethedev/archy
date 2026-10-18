"""Looking at the screen.

Two ways, picked by what the machine has. Wayland gets grim, the compositor's own
tool, piped straight through ffmpeg. Anywhere else, which means Windows, macOS and
X11, gets one frame from Pillow and straight to JPEG. Either way it is a
full-resolution JPEG handed to the model as an image, and nothing is written to
disk: the bytes exist only in memory.

Full resolution matters. A downscaled 1280 px JPEG misread the small text on the
dashboard in testing; the full frame reads it correctly. The JPEG keeps the
payload at about a tenth of the PNG's size, which the relay is happier with.
"""
import base64
import io
import shutil
import subprocess
import sys
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


def backend() -> str:
    """How this machine can be looked at, or an empty string if it cannot.

    grim first, because on Wayland it is the compositor's own tool and the only
    thing that reads a frame there. Then Pillow, which covers Windows, macOS and
    X11. The choice is by what is installed rather than by platform, so a Wayland
    session that also happens to have Pillow still gets grim.
    """
    if shutil.which("grim") and shutil.which("ffmpeg"):
        return "grim"
    try:
        import PIL.ImageGrab  # noqa: F401
    except ImportError:
        return ""
    return "grab"


def _capture_grim() -> bytes:
    png = _run(["grim", "-"])
    if not png:
        raise ScreenError("grim produced no image")
    # -f mjpeg with image2pipe keeps ffmpeg from wrapping the frames in a
    # container; the output is a bare jpeg
    return _run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0",
                 "-q:v", "85", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                stdin=png)


def _capture_grab() -> bytes:
    """One frame from Pillow, encoded in memory.

    all_screens is what asks for every monitor, and Pillow only accepts it on
    Windows, where it is also the default behaviour of a plain grab in recent
    versions. Passing it elsewhere raises, so it is passed where it applies.
    """
    from PIL import ImageGrab
    try:
        img = ImageGrab.grab(all_screens=True) if sys.platform == "win32" else ImageGrab.grab()
    except Exception as exc:                  # a headless session, no permission
        raise ScreenError(f"the screen grab failed: {exc}") from exc
    if img is None:
        raise ScreenError("the screen grab came back empty")
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=85)
    return out.getvalue()


def capture(attempts: int = 3) -> bytes:
    """Return a JPEG of the whole screen. Raises ScreenError on failure.

    Retried, because the compositor's screencopy fails on its own about one time
    in eight here: measured over 25 calls, 22 worked and 3 came back with
    "failed to copy output" in a fraction of a second, with a fullscreen video
    playing and nothing else in common. Each try is cheap, and retrying takes the
    rate to nearly certain from the caller's point of view, which matters because
    the alternative is answering a question about the screen while blind. The
    same retry covers a grab that comes back empty on a machine that is busy.
    """
    which = backend()
    if not which:
        raise ScreenError("nothing here can capture the screen: grim and ffmpeg on "
                          "Wayland, or Pillow anywhere else")
    grab = _capture_grim if which == "grim" else _capture_grab
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return grab()
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
