"""The durable store: it must persist, stay readable, and reach the prompt.

A memory layer that loses facts on restart, or that cannot be read by hand, or
that never makes it into what the model sees, fails the point of existing.
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis import config
from jarvis.memory_store import Memory
import jarvis.memory_store as ms

failures = 0
def check(label, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:46s} {detail}")

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "memory.json"

    m1 = Memory(path)
    check("starts empty", m1.line_count() == 0)
    m1.set("codeword", "purple lantern")
    m1.set("address_as", "dope")
    check("stores two facts", m1.line_count() == 2)

    m2 = Memory(path)                       # a fresh instance, as after a restart
    check("survives a restart", m2.get("codeword") == "purple lantern",
          repr(m2.get("codeword")))
    check("second fact intact", m2.get("address_as") == "dope")

    check("unknown key returns nothing", m2.get("nothing") is None)
    check("overwrite works", (m2.set("address_as", "boss"), m2.get("address_as"))[1] == "boss")
    check("forget works", m2.forget("address_as") and m2.get("address_as") is None)

    check("file is readable json", json.loads(path.read_text())["codeword"]["value"] == "purple lantern")

    # the summary is what actually reaches the model
    summary = m2.summary()
    check("summary mentions the fact", "codeword" in summary and "purple lantern" in summary, summary)

    # a corrupt file must not take the assistant down
    path.write_text("{ this is not json")
    m3 = Memory(path)
    check("a corrupt file degrades to empty", m3.line_count() == 0)
    m3.set("recovered", "yes")
    check("and can be written again", Memory(path).get("recovered") == "yes")

    # atomic write leaves no partial file behind
    check("no temporary file left over", not list(Path(tmp).glob("*.tmp")))

    # Two writers that both loaded before either wrote. This models the real
    # case: the assistant stores a fact by running this command, and that
    # process reads the file at startup. Whichever writes second must not
    # discard what it did not know about.
    conc = Path(tmp) / "conc.json"
    a = Memory(conc)
    b = Memory(conc)                      # loaded before a writes anything
    a.set("from_a", "1")
    b.set("from_b", "2")
    stored = Memory(conc).facts
    check("a fact written by another writer is not lost",
          {"from_a", "from_b"} <= set(stored), f"kept {sorted(stored)}")

    # and the same thing under real concurrency, six processes at once
    rush = Path(tmp) / "rush.json"
    env = {**os.environ, "JARVIS_MEMORY_FILE": str(rush)}
    procs = [subprocess.Popen(
        [sys.executable, "-m", "jarvis.memory_store", "set", f"k{i}", f"v{i}"],
        cwd=str(Path(__file__).resolve().parents[1]), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(6)]
    for p in procs:
        p.wait(timeout=90)
    stored = Memory(rush).facts
    missing = [f"k{i}" for i in range(6) if f"k{i}" not in stored]
    check("six writers at once all land", not missing,
          f"missing {missing}" if missing else f"kept all six")
    check("concurrent writes never corrupt the file",
          set(json.loads(rush.read_text())) == set(stored))

# ------------------------------------------------------------ the Windows lock
# Windows locks a byte range and refuses to lock a range it cannot see, so a lock
# file that starts empty raises instead of waiting. That is what lost a fact in
# the six-writer case on Windows while passing on Linux: the writer died quietly.
# The platform cannot be faked entirely, but the rule that broke it can.
class FakeMsvcrt:
    """Byte range locking, and the rule that matters: no range, no lock."""
    LK_LOCK, LK_UNLCK = 1, 2

    def __init__(self):
        self.taken, self.released = 0, 0

    def locking(self, fd, mode, n):
        if mode == self.LK_LOCK:
            if os.fstat(fd).st_size < 1:            # exactly what Windows does
                raise OSError(36, "Resource deadlock avoided")
            self.taken += 1
        else:
            self.released += 1


with tempfile.TemporaryDirectory() as locktmp:
    fake = FakeMsvcrt()
    real_fcntl, real_msvcrt = ms.fcntl, ms.msvcrt
    ms.fcntl, ms.msvcrt = None, fake
    try:
        lockfile = Path(locktmp) / "probe.json"
        lock = Path(locktmp) / ".probe.json.lock"
        with open(lock, "a+b") as fh:
            ms._lock(fh)
            check("a lock is taken even though the lock file started empty",
                  fake.taken == 1, f"taken={fake.taken}")
            ms._unlock(fh)
            check("and released", fake.released == 1, f"released={fake.released}")
        with open(lock, "a+b") as fh:
            ms._lock(fh)
            ms._unlock(fh)
            check("a second writer can lock the same file", fake.taken >= 2, f"taken={fake.taken}")
        probe = Memory(lockfile)
        probe.set("under_windows_lock", "yes")
        check("a write under the Windows lock lands", probe.get("under_windows_lock") == "yes")
        check("and the lock file was not truncated away",
              lock.stat().st_size >= 1, f"{lock.stat().st_size} bytes")
    finally:
        ms.fcntl, ms.msvcrt = real_fcntl, real_msvcrt

# ------------------------------------------------- the save has to be patient too
# On Windows a file another process is reading cannot be replaced, and six writers
# guarantee that overlap. The lock was already correct when this was found; the
# loss was in os.replace failing during someone else's read.
with tempfile.TemporaryDirectory() as repltmp:
    store = Memory(Path(repltmp) / "replacement.json")
    store.set("before", "1")
    real_replace = Path.replace
    waits = []

    def sometimes_blocked(self, target):
        waits.append(1)
        if len(waits) < 3:                          # as if a reader had it open
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(self, target)

    Path.replace = sometimes_blocked
    try:
        store.set("after", "2")
        check("a save that meets a reader waits and lands", store.get("after") == "2",
              f"took {len(waits)} attempts")
    except PermissionError as exc:
        check("a save that meets a reader waits and lands", False, f"gave up: {exc}")
    finally:
        Path.replace = real_replace
    check("and the fact is on disk, not just in memory",
          Memory(Path(repltmp) / "replacement.json").get("after") == "2")

# the command the model is told to run must actually work
out = subprocess.run([sys.executable, "-m", "jarvis.memory_store", "list"],
                     capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]))
check("module runs as a command", out.returncode == 0, out.stdout.strip().splitlines()[0] if out.stdout else out.stderr[-80:])

# and the prompt building must include the stored facts
from jarvis.brain import PiBrain
prompt = PiBrain._system_prompt()
check("prompt explains the store", "memory_store" in prompt)
check("prompt carries known facts", "What you already know about him" in prompt or "jarvis.memory_store list" in prompt)

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
