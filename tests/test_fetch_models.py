"""The model fetcher: does it land the right bytes, and does it refuse the wrong ones?

Every case here runs without a network. The download is exercised against a fake
server, which is the only way to test the failure that matters: a response that
stops early and would otherwise leave a half model that looks present.
"""
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from archy import fetch_models as fm                   # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += ok
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


class FakeResponse:
    """Enough of an http response to be read the way the fetcher reads one."""

    def __init__(self, body: bytes, declared: int | None):
        self._body = io.BytesIO(body)
        self.headers = {} if declared is None else {"Content-Length": str(declared)}

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def serve(body: bytes, declared: int | None = None, record: list | None = None):
    def urlopen(request, timeout=None):
        if record is not None:
            record.append(getattr(request, "full_url", request))
        return FakeResponse(body, declared if declared is not None else len(body))
    return urlopen


real_urlopen = fm.urllib.request.urlopen

print("-- what it plans to fetch --")
items = fm.plan()
check("three models are needed", len(items) == 3, ", ".join(i.name for i in items))
check("kokoro, its voices and the detector",
      [i.name for i in items] == ["kokoro-v1.0.onnx", "voices-v1.0.bin", "silero_vad.onnx"])
all_items = fm.plan(all_models=True)
check("--all adds the fallback voice and its settings",
      len(all_items) == 5 and all_items[-1].name.endswith(".json"))
check("the fallback is the only optional one",
      [i.name for i in all_items if i.optional] == ["en_GB-alan-medium.onnx",
                                                   "en_GB-alan-medium.onnx.json"])

print("-- a download that arrives whole --")
with tempfile.TemporaryDirectory() as tmp:
    target = Path(tmp) / "kokoro-v1.0.onnx"
    body = b"x" * 4096
    fm.urllib.request.urlopen = serve(body, record=[])
    try:
        line = fm.download(fm.plan()[0], target)
        check("the file is written", target.read_bytes() == body, line)
        check("and nothing is left beside it", not list(Path(tmp).glob("*.part")))
    except fm.FetchError as exc:
        check("the file is written", False, str(exc))
    finally:
        fm.urllib.request.urlopen = real_urlopen

print("-- a download that stops early --")
with tempfile.TemporaryDirectory() as tmp:
    target = Path(tmp) / "kokoro-v1.0.onnx"
    fm.urllib.request.urlopen = serve(b"x" * 100, declared=4096)   # promised 4096
    try:
        fm.download(fm.plan()[0], target)
        check("a short download is refused", False, "it accepted a truncated file")
    except fm.FetchError as exc:
        check("a short download is refused", "4096" in str(exc) and "100" in str(exc), str(exc)[:60])
        check("no half file is left behind",
              not target.exists() and not list(Path(tmp).glob("*.part")),
              f"{[p.name for p in Path(tmp).iterdir()]}")
    finally:
        fm.urllib.request.urlopen = real_urlopen

print("-- an empty response, and a server that is not there --")
with tempfile.TemporaryDirectory() as tmp:
    target = Path(tmp) / "silero_vad.onnx"
    fm.urllib.request.urlopen = serve(b"")
    try:
        fm.download(fm.plan()[2], target)
        check("an empty response is refused", False, "it wrote a zero byte model")
    except fm.FetchError:
        check("an empty response is refused", True)
    finally:
        fm.urllib.request.urlopen = real_urlopen

    def refuse(request, timeout=None):
        raise fm.urllib.error.URLError("no route to host")
    fm.urllib.request.urlopen = refuse
    try:
        fm.download(fm.plan()[2], target)
        check("a dead network is an error, not a crash", False)
    except fm.FetchError as exc:
        check("a dead network is an error, not a crash", "no route" in str(exc), str(exc)[:50])
    finally:
        fm.urllib.request.urlopen = real_urlopen

print("-- what is already here is left alone --")
with tempfile.TemporaryDirectory() as tmp:
    target = Path(tmp) / "voices-v1.0.bin"
    target.write_bytes(b"already")
    calls = []
    fm.urllib.request.urlopen = serve(b"new", record=calls)
    try:
        line = fm.download(fm.plan()[1], target)
        check("an existing file is not fetched again", not calls and target.read_bytes() == b"already",
              line)
        line = fm.download(fm.plan()[1], target, force=True)
        check("--force fetches over it", calls and target.read_bytes() == b"new", line)
    finally:
        fm.urllib.request.urlopen = real_urlopen

print("-- the model that ships inside a package --")
with tempfile.TemporaryDirectory() as tmp:
    target = Path(tmp) / "silero_vad.onnx"
    silero = fm.Item("silero_vad.onnx", None, "the detector", package_file="silero_vad.onnx")
    line = fm.copy_from_package(silero, target)
    check("it is copied out of openwakeword, not downloaded",
          target.exists() and line and "copied" in line, str(line)[:60])
    check("and it is the same bytes as the package has",
          target.read_bytes() == fm.in_package("silero_vad.onnx").read_bytes())

print("-- a file that is present but not usable --")
with tempfile.TemporaryDirectory() as tmp:
    models = Path(tmp)
    (models / "silero_vad.onnx").write_bytes(b"this is not an onnx file")
    complaint = fm.loads_ok(models, fm.plan()[2])
    check("a corrupt model is reported, not accepted", bool(complaint), complaint[:60])
    good = models / "good.json"
    good.write_text("{not json")
    check("a corrupt settings file too",
          bool(fm.loads_ok(models, fm.Item("good.json", "x", "settings"))))

print("-- --check, which is what an installer would use --")
with tempfile.TemporaryDirectory() as tmp:
    missing = fm.report(Path(tmp), fm.plan())
    check("it says what is missing", all("missing" in line for line in missing), missing[0])
    code = fm.main(["--check", "--dir", tmp])
    check("and it fails when something is missing", code == 1, f"exit {code}")
    for item in fm.plan():
        (Path(tmp) / item.name).write_bytes(b"x")
    lines = fm.report(Path(tmp), fm.plan())
    check("it says what is here", all("here" in line for line in lines))
    code = fm.main(["--check", "--dir", tmp])
    check("and passes when everything is present", code == 0, f"exit {code}")

print()
print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
sys.exit(1 if failed else 0)
