"""Editing a STEP assembly: a list of edit scripts applied to the original.

STEP is finished geometry exported from a CAD program (the Mark IV parts come
from Onshape), so there are no dimensions to turn. What can be done is what a
person would do to the assembly by hand: move, turn, scale or stretch parts,
delete them, add simple solids, cut holes. Each edit is a short script over
named parts (step_tool.py, run in the CadQuery interpreter). The original file
is never written: the edits replay on it for every preview, "undo" drops the
last one, and "save" writes a new STEP next to the original.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import time
from collections import OrderedDict
from typing import Dict, List, Optional

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = ROOT / "build" / "step_edit"
TOOL = pathlib.Path(__file__).resolve().parent / "step_tool.py"


def _run(python: str, request: dict, timeout: float = 600) -> dict:
    proc = subprocess.run([python, str(TOOL)], input=json.dumps(request), capture_output=True,
                          text=True, timeout=timeout)
    lines = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
    if not lines:
        return {"ok": False, "error": (proc.stderr or "no output").strip()[-600:]}
    return json.loads(lines[-1])


class StepSession:
    def __init__(self, path: pathlib.Path, python: str):
        self.path = path
        self.python = python
        self.scripts: List[Dict[str, str]] = []       # {"script", "say", "request"}
        self.history: List[List[Dict[str, str]]] = []
        self.last_error = ""
        self._inventory: Optional[dict] = None

    # ------------------------------------------------------------ parts

    def inventory(self) -> dict:
        """Parts of the ORIGINAL file, cached on its modification time."""
        if self._inventory is not None:
            return self._inventory
        CACHE.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(f"{self.path}:{self.path.stat().st_mtime_ns}".encode()).hexdigest()[:16]
        cached = CACHE / f"{self.path.stem}-{key}.inventory.json"
        if cached.exists():
            self._inventory = json.loads(cached.read_text())
            return self._inventory
        out = _run(self.python, {"cmd": "inventory", "src": str(self.path)})
        if "parts" not in out:
            raise RuntimeError(out.get("error", "inventory failed"))
        cached.write_text(json.dumps(out))
        self._inventory = out
        return out

    def summary(self, limit: int = 160) -> str:
        """Parts grouped by name (tube_1..tube_60 is one line), with extents,
        for the model writing the edit."""
        inv = self.inventory()
        groups: "OrderedDict[str, dict]" = OrderedDict()
        for p in inv["parts"]:
            g = groups.setdefault(p["group"], {"n": 0, "bb": None, "names": []})
            g["n"] += 1
            b = p["bbox"]
            g["bb"] = b if g["bb"] is None else [min(g["bb"][0], b[0]), min(g["bb"][1], b[1]),
                                                 min(g["bb"][2], b[2]), max(g["bb"][3], b[3]),
                                                 max(g["bb"][4], b[4]), max(g["bb"][5], b[5])]
            if len(g["names"]) < 2:
                g["names"].append(p["name"])
        rows = []
        for name, g in list(groups.items())[:limit]:
            label = f"{name}_* x{g['n']}" if g["n"] > 1 else g["names"][0]
            rows.append(f"{label}  bbox {g['bb']}")
        more = f"\n... and {len(groups) - limit} more groups" if len(groups) > limit else ""
        box = inv["bbox"]
        spans = [box[3] - box[0], box[4] - box[1], box[5] - box[2]]
        k = spans.index(max(spans))
        ax = "xyz"[k]
        lo_parts = sorted(groups.items(), key=lambda g: g[1]["bb"][k])[:3]
        hi_parts = sorted(groups.items(), key=lambda g: -g[1]["bb"][k + 3])[:3]
        orient = (f"Orientation: the long axis is {ax} ({box[k]:.0f} to {box[k + 3]:.0f} mm). "
                  f"Parts at the low-{ax} end: {', '.join(n for n, _ in lo_parts)}. "
                  f"At the high-{ax} end: {', '.join(n for n, _ in hi_parts)}. The user sees the model "
                  "turned by hand, so 'top' and 'bottom' are not reliable: read them relative to the "
                  "part they name (the bottom of a nozzle is its open exit end).")
        return (f"{len(inv['parts'])} parts, overall bbox (xmin, ymin, zmin, xmax, ymax, zmax) "
                f"{inv['bbox']} in mm.\n{orient}\n" + "\n".join(rows) + more)

    # ------------------------------------------------------------ edits

    def add(self, script: str, say: str, request: str) -> None:
        self.history.append(list(self.scripts))
        self.scripts.append({"script": script, "say": say, "request": request})

    def undo(self) -> bool:
        if not self.history:
            return False
        self.scripts = self.history.pop()
        return True

    def reset(self) -> None:
        if self.scripts:
            self.history.append(list(self.scripts))
        self.scripts = []

    @property
    def dirty(self) -> bool:
        return bool(self.scripts)

    def key(self, scripts: Optional[List[Dict[str, str]]] = None) -> str:
        body = f"{self.path}:{self.path.stat().st_mtime_ns}:" + "\n---\n".join(
            s["script"] for s in (self.scripts if scripts is None else scripts))
        return hashlib.sha1(body.encode()).hexdigest()[:16]

    def output(self, key: Optional[str] = None) -> pathlib.Path:
        return CACHE / f"{self.path.stem}-{key or self.key()}.stl"

    def try_script(self, script: str) -> dict:
        """Run the current edits plus a candidate; on success the preview for
        that state is cached, so accepting it costs nothing."""
        CACHE.mkdir(parents=True, exist_ok=True)
        candidate = self.scripts + [{"script": script, "say": "", "request": ""}]
        out = self.output(self.key(candidate))
        res = _run(self.python, {"cmd": "apply", "src": str(self.path),
                                 "scripts": [s["script"] for s in candidate], "stl": str(out)})
        if not res.get("ok") and out.exists():
            out.unlink()
        return res

    def build(self) -> Optional[pathlib.Path]:
        CACHE.mkdir(parents=True, exist_ok=True)
        out = self.output()
        if out.exists() and out.stat().st_size > 0:
            return out
        res = _run(self.python, {"cmd": "apply", "src": str(self.path),
                                 "scripts": [s["script"] for s in self.scripts], "stl": str(out)})
        if not res.get("ok"):
            self.last_error = res.get("error", "failed")
            return None
        return out

    def save(self) -> pathlib.Path:
        """A new STEP beside the original, named for the edit; never over it."""
        stamp = time.strftime("%Y%m%d-%H%M")
        dest = self.path.with_name(f"{self.path.stem}-jarvis-{stamp}.step")
        res = _run(self.python, {"cmd": "apply", "src": str(self.path),
                                 "scripts": [s["script"] for s in self.scripts],
                                 "stl": str(self.output()), "step": str(dest)})
        if not res.get("ok"):
            raise RuntimeError(res.get("error", "save failed"))
        notes = dest.with_suffix(".edits.txt")
        notes.write_text(f"Edits applied by J.A.R.V.I.S. to {self.path.name}, in order:\n\n" + "\n\n".join(
            f"{i + 1}. {s['request']}\n   {s['say']}\n{s['script']}" for i, s in enumerate(self.scripts)))
        return dest
