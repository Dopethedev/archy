"""Looking at the screen.

Three ways, picked by what the machine has. Wayland gets grim, the compositor's own
tool, piped straight through ffmpeg. macOS gets screencapture, which is the same
idea in Apple's form. Anywhere else, which means Windows and X11, gets one frame
from Pillow and straight to JPEG. Either way it is a full-resolution JPEG handed
to the model as an image, and nothing is written to disk for longer than the
encode: the bytes exist only in memory, and the temporary file screencapture
insists on is removed in a finally.

Full resolution matters. A downscaled 1280 px JPEG misread the small text on the
dashboard in testing; the full frame reads it correctly. The JPEG keeps the
payload at about a tenth of the PNG's size, which the relay is happier with.
"""
import base64
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config

PERMISSION_HINT = ("on macOS this is the Screen Recording permission: System "
                   "Settings, Privacy and Security, Screen Recording, and the app "
                   "running archy needs a tick there")


class ScreenError(Exception):
    """The screen could not be captured."""


def _run(argv: list[str], stdin: bytes | None = None, timeout: float = 10.0) -> bytes:
    """Run a capture tool. Everything that can go wrong arrives as a ScreenError.

    It did not: a timeout or a missing binary came out of subprocess as its own
    exception type, and the assistant's handler catches ScreenError, so a slow
    compositor produced "Something went wrong there" instead of "I could not
    capture the screen". A failure the caller cannot name is a failure nobody can
    act on.
    """
    try:
        proc = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ScreenError(f"{argv[0]} took longer than {timeout:.0f}s") from None
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScreenError(f"{argv[0]} could not be run: {exc}") from None
    if proc.returncode != 0:
        raise ScreenError(f"{argv[0]} failed: {proc.stderr.decode(errors='replace')[:120]}")
    return proc.stdout


def backend() -> str:
    """How this machine can be looked at, or an empty string if it cannot.

    grim first, because on Wayland it is the compositor's own tool and the only
    thing that reads a frame there. Then screencapture on macOS, which is the
    platform's own answer and does not need Pillow to be built with anything in
    particular. Then Pillow, which covers Windows and X11. The choice is by what
    is installed rather than by platform, so a Wayland session that also happens
    to have Pillow still gets grim.
    """
    if shutil.which("grim") and shutil.which("ffmpeg"):
        return "grim"
    if sys.platform == "darwin" and shutil.which("screencapture"):
        return "screencapture"
    try:
        import PIL.ImageGrab  # noqa: F401
    except ImportError:
        return ""
    return "grab"


def _to_jpeg(img) -> bytes:
    """A full-resolution jpeg from a Pillow image, in memory."""
    out = io.BytesIO()
    img.convert("RGB").save(out, format="JPEG", quality=85)
    return out.getvalue()


def _capture_screencapture() -> bytes:
    """One frame from screencapture, the way macOS takes a screenshot.

    -x is what stops it playing the shutter sound, which matters when the
    assistant is asked to look at the screen in a quiet room. It writes a file
    rather than a pipe, so the file is made, read and removed.

    Two failures share one cause and are told apart here. Without the Screen
    Recording permission screencapture exits non-zero, and on the versions where
    it exits zero it hands back a completely black frame instead. Neither is
    something the model can answer a question about, so both are reported as the
    permission problem they almost always are, which is a sentence the user can
    act on, rather than a black picture the model will invent a story about.
    """
    from PIL import Image
    handle, name = tempfile.mkstemp(suffix=".png")
    os.close(handle)
    path = Path(name)
    try:
        proc = subprocess.run(["screencapture", "-x", str(path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if proc.returncode != 0:
            detail = proc.stderr.decode(errors="replace").strip()[:120]
            raise ScreenError(f"screencapture failed ({detail or 'no message'}); "
                              f"{PERMISSION_HINT}")
        try:
            with Image.open(path) as img:
                frame = img.convert("RGB")
                if frame.convert("L").getextrema() == (0, 0):
                    raise ScreenError(f"the screen came back completely black, "
                                      f"and {PERMISSION_HINT}")
                return _to_jpeg(frame)
        except OSError as exc:
            raise ScreenError(f"screencapture wrote nothing that could be read: {exc}") from exc
    finally:
        path.unlink(missing_ok=True)


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
    return _to_jpeg(img)


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
                          "Wayland, screencapture on macOS, or Pillow anywhere else")
    grab = {"grim": _capture_grim, "screencapture": _capture_screencapture,
            "grab": _capture_grab}[which]
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return grab()
        except ScreenError as exc:
            last = exc
            if "permission" in str(exc).lower():
                # Asking a second and third time does not grant a permission, and
                # the sentence that says which setting it is is the whole value of
                # the answer, so it is not buried under "after 3 attempts".
                raise
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
