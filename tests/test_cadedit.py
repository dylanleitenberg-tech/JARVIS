"""Live CAD editing: parameters, spoken names, edits, builds, save, voice rules."""
from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jarvis import cadedit
from jarvis.ai import intents
from jarvis.models import OPENSCAD

FAILURES = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name} {detail}")


SRC = """// test part
width = 40;         // overall width [10:1:80]
wall_t = 2.5;       // wall thickness
n_holes = 4;        // hole count
explode = false;
derived = width * 2;
module part() { inner = 3; cube([width, wall_t * 4, 10]); }
width = 42;         // later assignment wins
part();
"""

tmp = pathlib.Path(tempfile.mkdtemp())
f = tmp / "part.scad"
f.write_text(SRC)
cadedit.CACHE = tmp / "cache"
cadedit.BACKUPS = tmp / "backups"

ps = cadedit.parameters(f)
names = [p.name for p in ps]
check("literal top-level assignments only", names == ["width", "wall_t", "n_holes", "explode"], str(names))
by = {p.name: p for p in ps}
check("later assignment wins", by["width"].value == 42.0 and by["width"].line == 7)
check("whole-number dimension is not a count", by["width"].kind == "number")
check("count by name steps by one", by["n_holes"].kind == "int" and by["n_holes"].bounds()[2] == 1.0)
check("customizer range read (from the winning line has none)", by["width"].bounds()[0] == 0.0)
check("bool", by["explode"].kind == "bool")

s = cadedit.EditSession(f, OPENSCAD)
check("spoken exact", s.find("wall t").name == "wall_t")
check("spoken partial", s.find("wall thickness").name == "wall_t")
check("spoken with article", s.find("the width").name == "width")
check("nonsense is None", s.find("banana split") is None)

s.set("wall_t", 3)
check("set", s.current("wall_t") == 3.0 and s.overrides == {"wall_t": 3.0})
s.nudge("width", True)
check("nudge default +10%", abs(s.current("width") - 46.2) < 1e-9, str(s.current("width")))
s.nudge("width", False, 2)
check("nudge by amount", abs(s.current("width") - 44.2) < 1e-9)
s.nudge("n_holes", True)
check("count nudges by one", s.current("n_holes") == 5)
s.set("explode", True)
check("undo", s.undo() and "explode" not in s.overrides)
for v in (41, 43, 45, 47):          # one drag
    s.set("width", v)
check("a drag is one undo step", s.undo() and abs(s.current("width") - 44.2) < 1e-9, str(s.current("width")))
s.set("wall_t", 2.5)
check("back to base drops the override", "wall_t" not in s.overrides)

if pathlib.Path(OPENSCAD).exists():
    t = time.time()
    out = s.compile()
    check("compiles with overrides", out is not None and out.stat().st_size > 84, s.last_error)
    check("fast", time.time() - t < 20, f"{time.time() - t:.1f}s")
    s.set("wall_t", -1e9)            # still valid OpenSCAD, just silly
    s.set("wall_t", 2.5)
else:
    print("  skip compile: no OpenSCAD")

before = f.read_text()
s.set("width", 50)
backup = s.save()
after = f.read_text()
check("save keeps a backup", backup is not None and backup.read_text() == before)
check("save rewrites only the winning line", after.splitlines()[7].startswith("width = 50;") and
      "later assignment wins" in after.splitlines()[7] and after.splitlines()[1] == before.splitlines()[1],
      after.splitlines()[7])
check("after save the value is the base", s.current("width") == 50.0 and not s.overrides)

# voice rules only claim an utterance when the part has that parameter
intents.param_lookup = s.find
intents.model_lookup = lambda name: None


def says(text, action, **want):
    got = intents.match(text)
    ok = got is not None and got[0] == action and all(got[1].get(k) == v for k, v in want.items())
    check(f"{text!r} -> {action}", ok, str(got))


says("set wall t to 3", "__cad_set", param="wall_t", value=3.0)
says("set the wall thickness to 3.5 mm", "__cad_set", param="wall_t", value=3.5)
says("make the width bigger", "__cad_nudge", param="width", up=True)
says("make the wall a bit thinner", "__cad_nudge", param="wall_t", up=False)
says("increase width by 20 percent", "__cad_nudge", param="width", amount=20.0, percent=True)
says("reduce width by 5", "__cad_nudge", param="width", amount=5.0, percent=False)
says("turn on explode", "__cad_bool", param="explode", on=True)
says("adjust the width", "__cad_adjust", param="width")
says("undo", "__cad_undo")
says("save changes", "__cad_save")
says("what can i change", "__cad_params")
says("a jarvis set width to 30", "__cad_set", param="width", value=30.0)
check("volume still reaches volume", intents.match("set volume to 50")[0] == "set_volume")
check("lower the volume is not an edit", intents.match("lower the volume") is None)

shutil.rmtree(tmp, ignore_errors=True)
print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED:"))
for failure in FAILURES:
    print(f"  {failure}")
sys.exit(1 if FAILURES else 0)
