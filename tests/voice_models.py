"""The voice models, for the groups that test with real speech.

Five groups here test what the assistant hears, and they test it with speech
rather than a tone, because a tone is not speech: each one synthesises its audio
with the same voice the assistant speaks with. Those models are 340 MB, they are
not in the repository, and the README fetches them after the first clone, so on a
checkout that has not run that step there is no fixture to test with.

That is a skip that names the command, not a traceback from five groups that look
broken. The assistant gives the same answer at runtime: with no voice installed it
reports that there is no usable speech engine instead of failing later and quietly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from archy import config              # noqa: E402

FETCH = "uv run python -m archy.fetch_models"
MODEL = config.MODELS / "kokoro-v1.0.onnx"
VOICES = config.MODELS / "voices-v1.0.bin"


def ready() -> bool:
    """Are both halves of the voice on disk: the model and the speaker list?"""
    return MODEL.exists() and VOICES.exists()


def require(what: str) -> None:
    """Leave with success when the voices are not fetched, saying why and how."""
    if ready():
        return
    print(f"skipped: the voice models are not fetched, and {what}")
    print(f"  {FETCH}   ({config.MODELS})")
    sys.exit(0)
