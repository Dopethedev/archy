"""The microphone switch.

What matters is that "off" really means off: the stream closed, the dashboard
saying so, and no path quietly treating it as a live microphone. The failure
case matters too, because a device that will not reopen must not leave the
assistant claiming it can hear you.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from archy.assistant import Assistant  # noqa: E402

failures = 0
def check(label, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:52s} {detail}")


class FakeMic:
    def __init__(self, opens=True):
        self.running = True
        self.opens = opens
        self.starts = 0
        self.stops = 0
        self.drains = 0
        self.focus = None
    def start(self):
        self.starts += 1
        if not self.opens:
            raise RuntimeError("device busy")
        self.running = True
    def stop(self):
        self.stops += 1
        self.running = False
    def drain(self):
        self.drains += 1
    def set_focus(self, on):
        self.focus = on


def build(opens=True):
    a = Assistant.__new__(Assistant)
    a.mic = FakeMic(opens=opens)
    a.hud = None
    a.wake = None
    a.state = "idle"
    a.mic_enabled = True
    a._loop = None
    a._stopping = False
    return a


a = build()
a.set_mic_enabled(False)
check("turning it off closes the stream", a.mic.running is False and a.mic.stops == 1,
      f"running={a.mic.running} stops={a.mic.stops}")
check("the dashboard is told it is off", a.state == "muted", f"state={a.state}")

# a turn finishing while muted must not claim it can hear you
a.set_state("speaking")
a._back_to_idle()
check("a reply ending while muted stays muted", a.state == "muted", f"state={a.state}")

a.set_mic_enabled(False)
check("turning it off twice does not close it twice", a.mic.stops == 1, f"stops={a.mic.stops}")

a.set_mic_enabled(True)
check("turning it on opens the stream", a.mic.running is True and a.mic.starts == 1,
      f"running={a.mic.running} starts={a.mic.starts}")
check("it goes back to listening once it can hear again", a.state == "idle", f"state={a.state}")

a.set_mic_enabled(True)
check("turning it on twice does not open a second stream", a.mic.starts == 1,
      f"starts={a.mic.starts}")

# a microphone that will not open is the interesting failure: the switch must not
# end up claiming it is on
b = build(opens=False)
b.set_mic_enabled(False)
import contextlib  # noqa: E402
import io  # noqa: E402
with contextlib.redirect_stdout(io.StringIO()):
    b.set_mic_enabled(True)
check("a microphone that will not open is reported, not assumed",
      b.mic_enabled is False and b.state == "muted",
      f"mic_enabled={b.mic_enabled} state={b.state}")

# and it can be tried again afterwards
b.mic.opens = True
b.set_mic_enabled(True)
check("and turning it on again works", b.mic.running and b.state == "idle",
      f"running={b.mic.running} state={b.state}")

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
