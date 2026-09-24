"""STEP editing: inventory, edits by name, the script safety check, undo, save."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jarvis import stepedit
from jarvis.models import ModelIndex

FAILURES = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name} {detail}")


py = ModelIndex([]).step_python()
if py is None:
    print("  skip: no Python with CadQuery")
    print("\nALL PASS")
    sys.exit(0)

tmp = pathlib.Path(tempfile.mkdtemp())
src = tmp / "assy.step"
subprocess.run([py, "-c", f"""
import cadquery as cq
a = cq.Assembly(name="assy")
a.add(cq.Workplane().box(10, 10, 100).translate((0, 0, -50)).val(), name="nozzle_tube_1")
a.add(cq.Workplane().box(10, 10, 100).translate((20, 0, -50)).val(), name="nozzle_tube_2")
a.add(cq.Workplane().cylinder(20, 30).translate((10, 0, 10)).val(), name="pump_housing")
(a.export if hasattr(a, "export") else a.save)({str(src)!r})
"""], check=True, capture_output=True)
stepedit.CACHE = tmp / "cache"

s = stepedit.StepSession(src, py)
inv = s.inventory()
names = [p["name"] for p in inv["parts"]]
check("inventory reads part names", sorted(names) == ["nozzle_tube_1", "nozzle_tube_2", "pump_housing"], str(names))
check("summary groups numbered parts", "nozzle_tube_* x2" in s.summary(), s.summary())

r = s.try_script("stretch('nozzle_tube_*', 'z', 1.5, anchor='max')")
check("stretch by name", r.get("ok"), str(r))
s.add("stretch('nozzle_tube_*', 'z', 1.5, anchor='max')", "nozzle 50% longer", "make the nozzle longer")
out = s.build()
check("preview builds", out is not None and out.stat().st_size > 84, s.last_error)

for bad in ("import os", "open('/etc/passwd')", "().__class__", "__import__('os')", "exec('1')"):
    r = s.try_script(bad)
    check(f"refused: {bad}", not r.get("ok") and "not allowed" in r.get("error", ""), str(r))
r = s.try_script("delete('no_such_part')")
check("unknown part is an error, not silence", not r.get("ok") and "no part matches" in r.get("error", ""))

dest = s.save()
check("save writes a new file beside the original", dest.exists() and dest != src and src.exists())
again = stepedit.StepSession(dest, py).inventory()
tube = [p for p in again["parts"] if p["name"] == "nozzle_tube_1"]
check("saved file keeps names", len(tube) == 1, str([p["name"] for p in again["parts"]]))
check("saved file has the edit (tube now 150 long, top still at 0)",
      tube and abs(tube[0]["bbox"][2] + 150) < 0.5 and abs(tube[0]["bbox"][5]) < 0.5, str(tube and tube[0]["bbox"]))
check("edit notes written", dest.with_suffix(".edits.txt").exists())

check("undo", s.undo() and not s.scripts)
import shutil
shutil.rmtree(tmp, ignore_errors=True)
print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED:"))
for failure in FAILURES:
    print(f"  {failure}")
sys.exit(1 if FAILURES else 0)
