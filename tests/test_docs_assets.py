"""The pictures in docs/ carry the project's name, and the checks can read it.

The header at the top of the README draws its wordmark as outlines, because a
README cannot load a font. A rename therefore missed it twice over: the word
"archy" had been written into the file's title text and nowhere else, the letters
themselves still spelled the old name, and the address under them still pointed
at the old repository. A search for the name finds nothing in a drawing, so the
missed banner stayed on the front page until someone read the picture.

The letters carry the character they draw now, which makes the name in the
picture a thing a check can read back without the font that drew it. The header
is the picture whose text can be read this way; for the PNGs, the same drift is
caught in the scripts that draw them.
"""
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
NAME = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
OLD_NAME = "jarvis"

failed = 0
passed = 0


def check(label, ok, detail=""):
    global failed, passed
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:56s} {detail}")


# ---------------------------------------------------------------- the banner
# Each outlined line is a group that records the text it is drawing, and every
# letter in it names its character. Read both back and compare.
banner = (DOCS / "banner.svg").read_text(encoding="utf-8")
lines = [(text, "".join(chars)) for text, chars in
         ((text, re.findall(r'data-char="([^"]*)"', body))
          for text, body in re.findall(r'<g data-text="([^"]*)"[^>]*>(.*?)</g>', banner, re.S))]

check("the header records the lines it draws", len(lines) == 4, f"{len(lines)} lines")
check("and it draws the characters it records",
      all(text.replace(" ", "") == "".join(chars) for text, chars in lines),
      "; ".join(f"{t[:16]!r} drew {''.join(c)!r}" for t, c in lines
                if t.replace(" ", "") != "".join(c)))

wordmark = "".join(lines[0][1]) if lines else ""
check("the wordmark spells the name the project has", wordmark == NAME,
      f"drew {wordmark!r}, project is {NAME!r}")

address = "".join(lines[-1][1]) if lines else ""
check("the address in the header is the project's",
      address.startswith("github.com/") and address.count("/") == 2
      and address.endswith("/" + NAME),
      f"drew {address!r}")

title = re.search(r"<title>([^<]*)</title>", banner)
check("the title of the file is that name too",
      bool(title) and title.group(1) == NAME,
      title.group(1) if title else "no title")

# ------------------------------------------------------- the scripts that draw
# A stale generator is how the banner went stale: the picture is generated, and
# regenerating from a script that still says the old name puts it back.
drawers = sorted([*DOCS.glob("render-*.py"), *DOCS.glob("render-*.sh")])
stale = [p.name for p in drawers if OLD_NAME in p.read_text(encoding="utf-8").lower()]
check("no script that draws a picture names the old project", not stale,
      f"{len(drawers)} scripts, stale: {', '.join(stale) or 'none'}")
check("and the header itself does not name it either",
      OLD_NAME not in banner.lower(), "the file, its text and its drawn strings")

print(f"\n{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
sys.exit(1 if failed else 0)
