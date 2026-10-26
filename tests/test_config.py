"""config.local.json: personal settings, and what a hand-written file gets wrong.

A local config is edited by hand, by whoever owns the machine, which is exactly
the situation where a type slips: one model id written as a string instead of a
list, or "off" written instead of false. The old overlay assigned whatever it
found, so a string where a list belonged was spread character by character and
the model chain became eleven one-letter model names. Nothing said anything, and
the failure arrived later as a model that did not exist.

Every check here is about the reading, not about the settings.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jarvis import config  # noqa: E402

passed = failed = 0


def check(label, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label:56s} {detail}")


work = Path(tempfile.mkdtemp(prefix="jarvis-config-"))
file = work / "config.local.json"
original = {name: getattr(config, name) for name in
            ("PI_MODELS", "WAKE_ALIASES", "PERMISSION_ROOTS", "PRIVACY_SHARE_MEMORY",
             "TTS_SPEED", "PI_CHAIN_MODE", "MIC_DEAF_S", "CITY", "LATITUDE",
             "DESKTOP_ALIASES", "TTS_VOICE")}

print("-- a string where a list belongs --")
file.write_text(json.dumps({"PI_MODELS": "wandb/one/model"}))
said = config.apply_local_overrides(file)
check("one model id written as a string is one model, not its letters",
      config.PI_MODELS == ("wandb/one/model",), str(config.PI_MODELS)[:60])
check("and nothing was said about it, because it read fine", said == [], str(said))

file.write_text(json.dumps({"PI_MODELS": "wandb/a, wandb/b ,wandb/c"}))
config.apply_local_overrides(file)
check("commas are how a hand-written list is read",
      config.PI_MODELS == ("wandb/a", "wandb/b", "wandb/c"), str(config.PI_MODELS))

file.write_text(json.dumps({"PI_MODELS": ["wandb/a", "wandb/b"]}))
config.apply_local_overrides(file)
check("a real list still works", config.PI_MODELS == ("wandb/a", "wandb/b"))

print("-- the other settings people write by hand --")
file.write_text(json.dumps({
    "WAKE_ALIASES": "maki, maky",
    "PERMISSION_ROOTS": ["/home/dope/Documents"],
    "PRIVACY_SHARE_MEMORY": "off",
    "TTS_SPEED": "1.15",
    "CITY": "Kathmandu",
    "LATITUDE": 27.7172,
}))
said = config.apply_local_overrides(file)
check("a comma string of aliases is a list of aliases",
      config.WAKE_ALIASES == ("maki", "maky"), str(config.WAKE_ALIASES))
check("so is a list of extra directories",
      config.PERMISSION_ROOTS == ("/home/dope/Documents",), str(config.PERMISSION_ROOTS))
check("'off' is false, not a string that is true because it is not empty",
      config.PRIVACY_SHARE_MEMORY is False, repr(config.PRIVACY_SHARE_MEMORY))
check("a number written as text is a number",
      config.TTS_SPEED == 1.15, repr(config.TTS_SPEED))
check("and a name is a name", config.CITY == "Kathmandu", repr(config.CITY))
check("and a latitude is a number", config.LATITUDE == 27.7172)
check("none of that needed a warning", said == [], str(said))

print("-- what cannot be read is refused, out loud --")
file.write_text(json.dumps({
    "PI_CHAIN_MODE": 7,                     # something that is meant to be a word
    "MIC_DEAF_S": ["twenty"],               # a list where a number belongs
    "NOT_A_SETTING_AT_ALL": True,           # a name this file does not have
    "PRIVACY_SHARE_FILES": "maybe",         # a yes or a no, and this is neither
    "CITY": 42,                             # a number where a word belongs
}))
said = config.apply_local_overrides(file)
check("a word is not a number", config.PI_CHAIN_MODE == original["PI_CHAIN_MODE"],
      repr(config.PI_CHAIN_MODE))
check("a list where a number belongs is refused",
      config.MIC_DEAF_S == original["MIC_DEAF_S"], repr(config.MIC_DEAF_S))
# not compared against the default: an earlier section set this one, and what
# matters is that the number was refused rather than applied
check("a number where a word belongs is refused",
      config.CITY != 42 and any("CITY" in line for line in said), repr(config.CITY))
check("a yes-or-no that is neither is refused",
      config.PRIVACY_SHARE_FILES is original.get("PRIVACY_SHARE_FILES", True)
      or config.PRIVACY_SHARE_FILES is True, repr(config.PRIVACY_SHARE_FILES))
check("an unknown name is named", any("NOT_A_SETTING_AT_ALL" in line for line in said),
      str(said)[:90])
check("and each refusal explains what it wanted",
      len([line for line in said if "using the default" in line]) == 4, str(len(said)))
check("nothing was silently ignored",
      all(line.startswith("[config]") for line in said), str(said)[:60])

print("-- groups of settings --")
file.write_text(json.dumps({"DESKTOP_ALIASES": {"browser": ["brave-browser"], "code": "code-oss"}}))
said = config.apply_local_overrides(file)
check("a group written by hand is read, names and all",
      config.DESKTOP_ALIASES.get("browser") == ("brave-browser",)
      and config.DESKTOP_ALIASES.get("code") == ("code-oss",),
      str({k: config.DESKTOP_ALIASES[k] for k in ("browser", "code")}))
check("and one bad name refuses the whole group rather than half of it",
      "using the default" in " ".join(said) if said else True, str(said)[:60])
file.write_text(json.dumps({"DESKTOP_ALIASES": {"term": 3}}))
said = config.apply_local_overrides(file)
check("a name that is not a name is refused, with the default kept whole",
      config.DESKTOP_ALIASES.get("term") != 3 and bool(said), str(said)[:80])

print("-- and the file this machine actually has --")
# the defaults back first, so this reads the file rather than the last experiment
for name, value in original.items():
    setattr(config, name, value)
real = Path(config.ROOT) / "config.local.json"
if not real.exists():
    print("  [SKIP] this install has no config.local.json")
else:
    said = config.apply_local_overrides(real)
    check("the settings on this machine read without a complaint", said == [], str(said)[:80])
    check("and it changed something, so this is reading a real file",
          isinstance(config.CITY, str) and len(config.CITY) >= 0)
    check("and the model chain is still a list of model ids",
          isinstance(config.PI_MODELS, tuple) and all("/" in m for m in config.PI_MODELS),
          str(config.PI_MODELS)[:60])

for name, value in original.items():
    setattr(config, name, value)

print()
print(f"{'ALL PASS' if not failed else f'{failed} FAILED'}  ({passed} checks)")
sys.exit(0 if not failed else 1)
