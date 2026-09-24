"""Edit a STEP assembly by script, in whichever Python has CadQuery.

Run as a subprocess by stepedit.py (the assistant's own environment does not
carry CadQuery). One JSON object on stdin, one on stdout:

  {"cmd": "inventory", "src": path}
      -> {"parts": [{"i", "name", "group", "bbox", "centre", "volume"}], "bbox"}
  {"cmd": "apply", "src": path, "scripts": [str, ...], "stl": path,
   "step": path | null, "tolerance": 0.8, "angular": 0.45}
      -> {"ok": true, "parts": n, "log": [...]} or {"ok": false, "error": str, "script": i}

A script is a few lines of Python that call the helpers below on `parts`, the
assembly's leaf parts in file order, each {"name", "shape"}. It runs with no
imports and no builtins beyond arithmetic and iteration, after an AST check,
because the text comes from a language model.
"""
from __future__ import annotations

import ast
import fnmatch
import json
import math
import re
import sys
import traceback

import cadquery as cq
from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform
from OCP.gp import gp_GTrsf, gp_Mat, gp_XYZ
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.XCAFDoc import XCAFDoc_DocumentTool


# ------------------------------------------------------------------ reading

def load_parts(src: str):
    """Leaf parts with their names and placed shapes, assemblies flattened."""
    doc = TDocStd_Document(TCollection_ExtendedString("doc"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    if reader.ReadFile(src) != IFSelect_RetDone:
        raise RuntimeError(f"cannot read {src}")
    reader.Transfer(doc)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    free = TDF_LabelSequence()
    tool.GetFreeShapes(free)
    parts = []

    def name_of(label) -> str:
        attr = TDataStd_Name()
        return attr.Get().ToExtString() if label.FindAttribute(TDataStd_Name.GetID_s(), attr) else ""

    def walk(label, loc, parent=""):
        ref = label
        if tool.IsReference_s(label):
            ref = TDF_Label()
            tool.GetReferredShape_s(label, ref)
            loc = loc * tool.GetLocation_s(label)
        inst, prod = name_of(label), name_of(ref)
        # Instance names are often bare ids ("1", "3"); the product name is the
        # real one. A CadQuery export also wraps each part as name -> name_part.
        name = prod if (not inst or inst.isdigit()) else inst
        if parent and name == parent + "_part":
            name = parent
        if tool.IsAssembly_s(ref):
            comps = TDF_LabelSequence()
            tool.GetComponents_s(ref, comps)
            for i in range(1, comps.Length() + 1):
                walk(comps.Value(i), loc, name)
        else:
            shape = cq.Shape.cast(tool.GetShape_s(ref).Moved(loc))
            parts.append({"name": name or f"part_{len(parts)}", "shape": shape})

    for i in range(1, free.Length() + 1):
        walk(free.Value(i), TopLoc_Location())
    if not parts:        # no assembly structure at all: one shape
        parts.append({"name": "body", "shape": cq.importers.importStep(src).val()})
    return parts


def group_of(name: str) -> str:
    """eng_rn_regen_tube_up_15 -> eng_rn_regen_tube_up"""
    return re.sub(r"[_\-\s]*\d+$", "", name) or name


def inventory(src: str) -> dict:
    parts = load_parts(src)
    rows = []
    allbb = None
    for i, p in enumerate(parts):
        bb = p["shape"].BoundingBox()
        allbb = bb if allbb is None else allbb.add(bb)
        rows.append({"i": i, "name": p["name"], "group": group_of(p["name"]),
                     "bbox": [round(v, 1) for v in (bb.xmin, bb.ymin, bb.zmin, bb.xmax, bb.ymax, bb.zmax)],
                     "centre": [round(v, 1) for v in (bb.center.x, bb.center.y, bb.center.z)],
                     "volume": round(p["shape"].Volume(), 1)})
    box = [round(v, 1) for v in (allbb.xmin, allbb.ymin, allbb.zmin, allbb.xmax, allbb.ymax, allbb.zmax)] \
        if allbb else []
    return {"parts": rows, "bbox": box}


# ------------------------------------------------------------------ helpers

class Edit:
    """The operations a script may call. `sel` is a name pattern ("eng_rn_*",
    a regex written as "re:..."), a list of indices, or one index."""

    def __init__(self, parts, log):
        self.parts = parts
        self.log = log

    def _pick(self, sel):
        if isinstance(sel, int):
            return [sel]
        if isinstance(sel, (list, tuple)):
            return [int(i) for i in sel]
        pat = str(sel)
        if pat.startswith("re:"):
            rx = re.compile(pat[3:], re.I)
            hits = [i for i, p in enumerate(self.parts) if rx.search(p["name"])]
        else:
            hits = [i for i, p in enumerate(self.parts) if fnmatch.fnmatch(p["name"].lower(), pat.lower())]
        if not hits:
            raise ValueError(f"no part matches {sel!r}")
        return hits

    def select(self, sel):
        return self._pick(sel)

    def names(self, sel="*"):
        return [self.parts[i]["name"] for i in self._pick(sel)]

    def bbox(self, sel="*"):
        """(xmin, ymin, zmin, xmax, ymax, zmax) of the selection."""
        bb = None
        for i in self._pick(sel):
            b = self.parts[i]["shape"].BoundingBox()
            bb = b if bb is None else bb.add(b)
        return (bb.xmin, bb.ymin, bb.zmin, bb.xmax, bb.ymax, bb.zmax)

    def move(self, sel, dx=0.0, dy=0.0, dz=0.0):
        idx = self._pick(sel)
        for i in idx:
            self.parts[i]["shape"] = self.parts[i]["shape"].translate(cq.Vector(dx, dy, dz))
        self.log.append(f"moved {len(idx)} part(s) by ({dx:g}, {dy:g}, {dz:g})")

    def rotate(self, sel, axis="z", degrees=0.0, about=None):
        """Turn about an axis ("x", "y", "z" or a direction tuple) through
        `about` (default: the selection's centre)."""
        idx = self._pick(sel)
        d = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}.get(axis, axis)
        if about is None:
            b = self.bbox(idx)
            about = ((b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2)
        a = cq.Vector(*about)
        for i in idx:
            self.parts[i]["shape"] = self.parts[i]["shape"].rotate(a, a + cq.Vector(*d), degrees)
        self.log.append(f"rotated {len(idx)} part(s) {degrees:g} deg about {axis}")

    def scale(self, sel, factor, about=None):
        """Uniform scale about `about` (default: the selection's centre)."""
        self.stretch(sel, "xyz", factor, about)

    def stretch(self, sel, axis="z", factor=1.0, anchor="centre"):
        """Scale along one axis ("x", "y", "z", or "xyz" for all), holding
        `anchor` fixed: "min", "max", "centre", or a coordinate on that axis.
        "Make the nozzle 20% longer, keeping where it joins" is
        stretch("nozzle*", "z", 1.2, anchor="max") when the joint is at the top."""
        idx = self._pick(sel)
        b = self.bbox(idx)
        axes = "xyz" if axis in ("xyz", "all") else axis
        m = gp_Mat(1, 0, 0, 0, 1, 0, 0, 0, 1)
        shift = [0.0, 0.0, 0.0]
        for k, ax in enumerate("xyz"):
            if ax not in axes:
                continue
            lo, hi = b[k], b[k + 3]
            if isinstance(anchor, (int, float)):
                a = float(anchor)
            elif isinstance(anchor, (list, tuple)):
                a = float(anchor[k])
            else:
                a = {"min": lo, "max": hi}.get(str(anchor), (lo + hi) / 2)
            m.SetValue(k + 1, k + 1, float(factor))
            shift[k] = a * (1 - float(factor))
        g = gp_GTrsf()
        g.SetVectorialPart(m)
        g.SetTranslationPart(gp_XYZ(*shift))
        for i in idx:
            op = BRepBuilderAPI_GTransform(self.parts[i]["shape"].wrapped, g, True)
            self.parts[i]["shape"] = cq.Shape.cast(op.Shape())
        self.log.append(f"stretched {len(idx)} part(s) x{factor:g} along {axis}")

    def delete(self, sel):
        idx = set(self._pick(sel))
        self.parts[:] = [p for i, p in enumerate(self.parts) if i not in idx]
        self.log.append(f"deleted {len(idx)} part(s)")

    def add(self, name, shape):
        """Add a new solid: shape is a cq.Workplane or a cq.Shape, e.g.
        cq.Workplane("XY").cylinder(40, 10).translate((0, 0, -820))."""
        if isinstance(shape, cq.Workplane):
            shape = shape.val()
        self.parts.append({"name": str(name), "shape": shape})
        self.log.append(f"added {name}")

    def copy(self, sel, dx=0.0, dy=0.0, dz=0.0, suffix="_copy"):
        for i in self._pick(sel):
            p = self.parts[i]
            self.parts.append({"name": p["name"] + suffix,
                               "shape": p["shape"].translate(cq.Vector(dx, dy, dz))})
        self.log.append("copied parts")

    def mirror(self, sel, plane="YZ", about=(0, 0, 0)):
        idx = self._pick(sel)
        for i in idx:
            self.parts[i]["shape"] = self.parts[i]["shape"].mirror(plane, cq.Vector(*about))
        self.log.append(f"mirrored {len(idx)} part(s) in {plane}")

    def fillet(self, sel, radius):
        """Round every edge of the selected parts; fails on edges too short."""
        idx = self._pick(sel)
        for i in idx:
            s = self.parts[i]["shape"]
            self.parts[i]["shape"] = s.fillet(radius, s.Edges())
        self.log.append(f"filleted {len(idx)} part(s) r{radius:g}")

    def cut(self, sel, tool):
        """Subtract a shape (hole, slot) from the selected parts."""
        if isinstance(tool, cq.Workplane):
            tool = tool.val()
        idx = self._pick(sel)
        for i in idx:
            self.parts[i]["shape"] = self.parts[i]["shape"].cut(tool)
        self.log.append(f"cut {len(idx)} part(s)")

    def union(self, sel, tool):
        if isinstance(tool, cq.Workplane):
            tool = tool.val()
        idx = self._pick(sel)
        for i in idx:
            self.parts[i]["shape"] = self.parts[i]["shape"].fuse(tool)
        self.log.append(f"joined a shape to {len(idx)} part(s)")


# ------------------------------------------------------------------ safety

_BANNED_CALLS = {"open", "exec", "eval", "compile", "getattr", "setattr", "delattr", "globals",
                 "locals", "vars", "input", "__import__", "breakpoint", "help", "exit", "quit"}


def check(script: str) -> None:
    tree = ast.parse(script)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal,
                             ast.AsyncFunctionDef, ast.Await, ast.ClassDef)):
            raise ValueError(f"not allowed in an edit script: {type(node).__name__}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(f"not allowed: .{node.attr}")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError(f"not allowed: {node.id}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _BANNED_CALLS:
            raise ValueError(f"not allowed: {node.func.id}()")


_SAFE_BUILTINS = {k: __builtins__[k] if isinstance(__builtins__, dict) else getattr(__builtins__, k)
                  for k in ("range", "len", "min", "max", "abs", "round", "float", "int", "list",
                            "dict", "tuple", "enumerate", "zip", "sorted", "sum", "any", "all",
                            "str", "bool", "reversed", "ValueError", "print")}


def run_script(parts, script: str, log) -> None:
    check(script)
    ed = Edit(parts, log)
    env = {"__builtins__": _SAFE_BUILTINS, "cq": cq, "math": math, "parts": parts}
    for name in ("select", "names", "bbox", "move", "rotate", "scale", "stretch", "delete",
                 "add", "copy", "mirror", "fillet", "cut", "union"):
        env[name] = getattr(ed, name)
    exec(compile(script, "<edit>", "exec"), env)


def export(parts, stl: str, step, tolerance: float, angular: float) -> None:
    compound = cq.Compound.makeCompound([p["shape"] for p in parts])
    cq.exporters.export(compound, stl, tolerance=tolerance, angularTolerance=angular,
                        exportType="STL", opt={"ascii": False})
    if step:
        assy = cq.Assembly(name="edited")
        for p in parts:
            assy.add(p["shape"], name=p["name"])
        (assy.export if hasattr(assy, "export") else assy.save)(step)


def main() -> int:
    req = json.load(sys.stdin)
    try:
        if req["cmd"] == "inventory":
            out = inventory(req["src"])
        elif req["cmd"] == "apply":
            parts = load_parts(req["src"])
            log = []
            for n, script in enumerate(req.get("scripts") or []):
                try:
                    run_script(parts, script, log)
                except Exception as exc:
                    print(json.dumps({"ok": False, "script": n,
                                      "error": f"{type(exc).__name__}: {exc}"[-600:],
                                      "trace": traceback.format_exc()[-1200:]}))
                    return 0
            export(parts, req["stl"], req.get("step"), float(req.get("tolerance", 0.8)),
                   float(req.get("angular", 0.45)))
            out = {"ok": True, "parts": len(parts), "log": log}
        else:
            out = {"ok": False, "error": "unknown cmd"}
    except Exception as exc:
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[-600:]}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
