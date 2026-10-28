"""Interrupting a reply must stop it, not just quieten it for a moment.

A reply is spoken sentence by sentence, and synthesising one sentence takes
seconds. So at the moment the user cuts in, the speaker is almost always in the
middle of that work. It used to be queued regardless of the flush, which meant a
fragment of the answer the user had just stopped arrived seconds later and
played over the reply they had asked for instead: the glitch.

Nothing here touches an audio device.
"""
from pathlib import Path
import collections
import queue
import re
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np  # noqa: E402
from archy import config  # noqa: E402
from archy.audio import Player  # noqa: E402
from archy.tts import Speaker, split_ready, clean  # noqa: E402

failures = 0
def check(label, ok, detail):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:52s} {detail}")


# ------------------------------------------------------------------ the player
p = Player()
gen = p.start_reply()
check("a reply's own audio is accepted", p.write(np.ones(64, np.float32), gen),
      f"playing={p.playing()}")

p.flush()                                    # the user interrupts
check("the queue is dropped on interrupt", not p.playing(), f"playing={p.playing()}")

late = p.write(np.ones(64, np.float32), gen)
check("a sentence still being synthesised is refused", not late and not p.playing(),
      f"accepted={late} playing={p.playing()}")

gen2 = p.start_reply()
check("the next reply is accepted", p.write(np.ones(64, np.float32), gen2) and p.playing(),
      f"playing={p.playing()}")

print("-- drained means played, not merely handed over --")
# The queue emptying is not the sound stopping: the block just handed to the device
# is in its buffer and plays after the callback returns. Every reader of "the reply
# is over" treated an empty queue as finished, so the microphone went back to
# listening over the last word of the answer, and a reminder could start speaking
# into the tail of one.
device = Player.__new__(Player)
device.samplerate = config.HUD_RATE
device._chunks = collections.deque([np.full(2048, 0.5, dtype=np.float32)])
device._lock = threading.Lock()
device._needs_fade = True
device._channels = 2
device._generation = 0
device._speaking = False
device.level = 0.0
out = np.zeros((1024, 2), dtype=np.float32)

device._callback(out, 1024, None, None)
check("half the audio is still queued, so it is playing",
      device.playing(), "the first block left the queue")
check("and it said something, so the level moved", device.level > 0.0, f"{device.level:.3f}")

device._callback(out, 1024, None, None)
check("a block in the device, not yet heard, still counts as playing",
      device.playing(), "the queue is empty here and the sound is not")

device._callback(out, 1024, None, None)
check("and a silent callback is the end of it", not device.playing(),
      "everything queued has been played")
check("an interrupted reply is over at once",
      (device.write(np.ones(64, np.float32)), device.flush(), not device.playing())[-1],
      "a flush drops the queue and the speaker's own idea of playing")


# ------------------------------------------------- and through the real speaker
class SlowSpeaker(Speaker):
    """Stands in for kokoro: a sentence takes a measurable moment to produce."""

    SYNTH_SECONDS = 0.4

    def __init__(self, player):
        self.player = player
        self.engine = "stub"
        self.synth_calls = 0

    def synth(self, text):
        self.synth_calls += 1
        time.sleep(self.SYNTH_SECONDS)
        return np.full(config.HUD_RATE // 2, 0.1, dtype=np.float32)


deltas: queue.Queue = queue.Queue()
player = Player()
speaker = SlowSpeaker(player)
spoken = threading.Event()
stop = threading.Event()

worker = threading.Thread(
    target=speaker.say_stream,
    args=((d for d in iter(deltas.get, None)), spoken.set, stop),
    daemon=True,
)
worker.start()
for piece in ["Here is the start of a reply, ", "and this is the sentence ", "that is being synthesised when you cut in."]:
    deltas.put(piece)
time.sleep(0.5)                              # first chunk spoken, second mid synthesis

started_first = spoken.is_set()
stop.set()                                   # the interrupt
player.flush()
deltas.put(None)
worker.join(timeout=5)

check("the first chunk was spoken before the interrupt", started_first,
      f"spoken={started_first}")
check("nothing is left playing after the interrupt", not player.playing(),
      f"playing={player.playing()}")
check("the interrupted thread stopped by itself", not worker.is_alive(),
      f"alive={worker.is_alive()}")


# ------------------------------------------------------------- the lag it bounds
long_line = ("It is worth saying that a sentence which keeps going without any "
             "punctuation at all will eventually be broken somewhere, because "
             "otherwise the synthesiser holds on to it for several seconds")
chunks = []
buffer = long_line
while buffer:
    ready, buffer = split_ready(buffer, first=not chunks)
    if not ready:
        chunks.append(clean(buffer))         # the tail, exactly what say_stream speaks
        break
    chunks.append(ready)
print(f"    (a {len(long_line)} character sentence became {len(chunks)} chunks: "
      f"{[len(c) for c in chunks]})")
check("no chunk is long enough to hold synthesis for seconds",
      max(len(c) for c in chunks) <= 90, f"longest {max(len(c) for c in chunks)} chars")
check("the opening chunk is short even when the only comma is late",
      len(chunks[0]) <= 48, f"opening chunk {len(chunks[0])} chars")
words = lambda t: re.sub(r"\s+", " ", t).strip()
check("no word is lost splitting a long sentence",
      words(" ".join(chunks)) == words(long_line),
      f"{len(words(' '.join(chunks)).split())} words against "
      f"{len(words(long_line).split())}")

print(f"\n{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
