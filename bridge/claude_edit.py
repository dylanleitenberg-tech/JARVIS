#!/usr/bin/env python3
"""Turn a spoken CAD change into an edit, through the Claude Code CLI.

stdin  {"mode": "scad", "request": str, "file": name, "source": str, "error": str|null,
        "previous": str|null}
       {"mode": "step", "request": str, "file": name, "parts": str (inventory summary),
        "applied": [str], "error": str|null, "previous": str|null}
stdout {"say": str, "edits": [{"find": str, "replace": str}]}      (scad)
       {"say": str, "script": str}                                  (step)
       {"say": str, "question": true}          when it needs to ask instead

`error` and `previous` carry a failed first attempt back for one repair try.
Same isolation as claude_code.py: a temporary working directory, no project
settings, no MCP servers, no tools; the model only returns text.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from claude_code import extract_json, find_binary  # noqa: E402

TIMEOUT = float(os.environ.get("JARVIS_CLAUDE_EDIT_TIMEOUT", "180"))
MODEL = os.environ.get("JARVIS_CLAUDE_EDIT_MODEL", "sonnet")

COMMON = """
You edit CAD for a voice assistant. The user spoke a change; make exactly that
change and nothing else. Reply with ONE JSON object, no prose, no code fence.
"say" is spoken aloud: one short sentence saying what you changed, with the
numbers that matter. If the request is ambiguous in a way that changes the
result (which part, how much, which side), do not guess: reply
{"say": "<one short question>", "question": true}. A request for "a bit" or
"slightly" means about 10 to 20 percent; pick a sensible amount and say it.
""".strip()

SCAD = """
The part is an OpenSCAD file. Return
  {"say": "...", "edits": [{"find": "<exact text now in the file>", "replace": "<new text>"}]}
Each "find" must be copied exactly from the file and occur exactly once; keep
it short but unique (a whole line or a few lines). Keep the file's style and
comments. Prefer changing or adding named top-level variables over magic
numbers, so the change stays editable. The result must compile in OpenSCAD.
""".strip()

STEP = """
The part is a STEP assembly of named solids in millimetres (listed below with
bounding boxes xmin, ymin, zmin, xmax, ymax, zmax). Nothing in it is
parametric; you edit it with a short Python script that calls ONLY these
helpers, with no imports:
  select(sel) -> [indices]          sel: glob on part names ("eng_rn_*"),
                                     "re:<regex>", an index, or a list of indices
  names(sel), bbox(sel) -> (xmin, ymin, zmin, xmax, ymax, zmax)
  move(sel, dx, dy, dz)
  rotate(sel, axis="z", degrees, about=None)      about: point, default centre
  scale(sel, factor, about=None)
  stretch(sel, axis, factor, anchor)  scale along "x"/"y"/"z" holding anchor
                                     fixed: "min", "max", "centre" or a coordinate
  delete(sel)
  add(name, shape)                   shape: cq.Workplane(...) solid, placed in mm
  copy(sel, dx, dy, dz), mirror(sel, plane="YZ", about=(0,0,0))
  cut(sel, tool), union(sel, tool)   tool: cq.Workplane solid (holes, bosses)
  fillet(sel, radius)                rounds every edge of the selected parts
cq (CadQuery) and math are available. Filter selections with bbox() when a
name pattern catches too much (for example only the parts below a joint).
Keep joints connected: when you stretch or move one section, move what is
attached to its free end with it. Return
  {"say": "...", "script": "<python>"}
""".strip()


def main() -> int:
    req = json.load(sys.stdin)
    mode = req.get("mode")
    if mode == "scad":
        system = "\n\n".join([COMMON, SCAD])
        prompt = (f"File: {req.get('file')}\n\n----- source -----\n{req.get('source', '')}\n----- end -----\n\n"
                  f"Change requested: {req.get('request')}")
    elif mode == "step":
        system = "\n\n".join([COMMON, STEP])
        applied = req.get("applied") or []
        done = ("\n\nEdits already applied, in order (the part list is the ORIGINAL file; "
                "these ran on it first):\n" + "\n---\n".join(applied)) if applied else ""
        prompt = (f"Assembly: {req.get('file')}\n{req.get('parts', '')}{done}\n\n"
                  f"Change requested: {req.get('request')}")
    else:
        print(json.dumps({"say": "I do not know how to edit that kind of file."}))
        return 0
    if req.get("error"):
        prompt += (f"\n\nYour previous attempt failed.\nAttempt:\n{req.get('previous', '')}\n"
                   f"Error:\n{req['error']}\nFix it.")

    with tempfile.TemporaryDirectory(prefix="jarvis-edit-") as sandbox:
        try:
            proc = subprocess.run(
                [find_binary(), "-p", prompt, "--system-prompt", system,
                 "--output-format", "text", "--model", MODEL,
                 "--setting-sources", "", "--strict-mcp-config", "--allowed-tools", ""],
                capture_output=True, text=True, timeout=TIMEOUT, cwd=sandbox)
        except subprocess.TimeoutExpired:
            print(json.dumps({"say": "That edit took too long; I stopped.", "failed": True}))
            return 0
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        print(json.dumps({"say": "My link to Claude Code failed.", "failed": True,
                          "error": tail[-1] if tail else ""}))
        return 0
    out = extract_json(proc.stdout)
    if not isinstance(out, dict):
        out = {"say": str(out)[:300]}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
