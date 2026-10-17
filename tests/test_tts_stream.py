"""Speaking starts on a clause, not a sentence, and nothing is lost doing it.

Kokoro synthesises at roughly real time. Waiting for a whole sentence before
speaking therefore costs that sentence in silence, which was 2.77s of the 4.26s
it took to say anything at all. The risk of cutting earlier is dropping or
duplicating words, so that is checked too.
"""
from pathlib import Path
import re, sys, time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import numpy as np
from jarvis.tts import Speaker, clean, split_ready

failures = 0
def check(label, ok, detail=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:50s} {detail}")

# ------------------------------------------------------------------ the rule
check("the opening clause is enough to start",
      tuple(p.strip() for p in split_ready("Your CPU usage, is about four percent.", first=True))
      == ("Your CPU usage,", "is about four percent."),
      repr(split_ready("Your CPU usage, is about four percent.", first=True)))

check("a short opening like 'Yes,' is not enough on its own",
      split_ready("Yes, it is fine.", first=True)[0].strip() != "Yes,",
      repr(split_ready("Yes, it is fine.", first=True)))

check("later chunks wait for a sentence",
      split_ready("Your CPU usage, is about four percent. And", first=False)[0].strip()
      == "Your CPU usage, is about four percent.",
      repr(split_ready("Your CPU usage, is about four percent. And", first=False)))

check("a sentence with no comma still works",
      split_ready("Sober is open now.", first=True)[0].strip() == "Sober is open now.",
      repr(split_ready("Sober is open now.", first=True)))


# --------------------------------------------------- the timing it actually buys
SENTENCE = "Your CPU usage is hovering around four percent, which is entirely normal."
CHARS_PER_SECOND = 40.0          # kokoro, measured at about 1x real time


class FakePlayer:
    """Models the real player: replies carry a generation, and audio from an
    older one is refused."""

    def __init__(self):
        self.first_at = None
        self.chunks = []
        self.generation = 0
        self.refused = 0

    def start_reply(self):
        self.generation += 1
        return self.generation

    def write(self, audio, generation=None):
        if generation is not None and generation != self.generation:
            self.refused += 1
            return False
        if self.first_at is None:
            self.first_at = time.monotonic()
        self.chunks.append(audio.size)
        return True


def stream(first_cut: bool):
    """Speak the sentence as the model streams it, word by word."""
    sp = Speaker.__new__(Speaker)
    sp.player = FakePlayer()
    sp.engine = "stub"
    spoken = []
    sp.spoken = spoken

    def synth(text):
        time.sleep(len(text) / CHARS_PER_SECOND)     # cost proportional to length
        spoken.append(text)
        return np.zeros(int(48000 * len(text) / CHARS_PER_SECOND), dtype=np.float32)
    sp.synth = synth

    deltas = iter(word + " " for word in SENTENCE.split())
    started = time.monotonic()
    if first_cut:
        sp.say_stream(deltas)
    else:
        # the old behaviour: every chunk waited for a full sentence
        buffer = ""
        for delta in deltas:
            buffer += delta
            ready, buffer = split_ready(buffer, first=False)
            if ready and (text := clean(ready)):
                sp.synth(text)
                sp.player.write(np.zeros(1, dtype=np.float32))
        if tail := clean(buffer):
            sp.synth(tail)
            sp.player.write(np.zeros(1, dtype=np.float32))
    return sp.player.first_at - started, spoken


fast, chunks_fast = stream(first_cut=True)
slow, chunks_slow = stream(first_cut=False)

check("speaking starts sooner", fast < slow * 0.7,
      f"{fast:.2f}s against {slow:.2f}s for a {len(SENTENCE)} char sentence")

joined = re.sub(r"\s+", " ", " ".join(chunks_fast)).strip()
check("no word is lost or duplicated", joined == SENTENCE, repr(joined))
check("it still says all of it", len(chunks_fast) >= 2, f"{len(chunks_fast)} chunks")

print(f"\nspeaking began after {fast:.2f}s instead of {slow:.2f}s")
print(f"{'ALL PASS' if failures == 0 else str(failures) + ' FAILED'}")
sys.exit(1 if failures else 0)
