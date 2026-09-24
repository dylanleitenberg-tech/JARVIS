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
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from claude_code import extract_json, find_binary  # noqa: E402

TIMEOUT = float(os.environ.get("JARVIS_CLAUDE_EDIT_TIMEOUT", "300"))
# Opus 5.5, because this is the one call in the system that has to reason
# about shape: which named solids a phrase like "the nozzle skirt" covers,
# what is attached to what, and which anchor keeps a joint closed. Sonnet
# answered "make the bell 30% bigger" by scaling overlapping selections in
# successive edits and tore the assembly into a starburst.
#
# The id is claude-opus-5-5. Note the hyphens: "claude-opus-5.5" and
# "opus-5.5" are both rejected by the CLI, the first as a model that may not
# exist and the second as absent from the version's catalogue.
MODEL = os.environ.get("JARVIS_CLAUDE_EDIT_MODEL", "claude-opus-5-5")

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
  revolve(name, profile, wall=None, axis="z", centre=(0, 0))
                                     solid (or shell of thickness wall) of
                                     revolution; profile = [(radius, height), ...]
                                     along the axis, heights in the same
                                     coordinates as the bboxes. USE THIS for any
                                     round part; never build revolves by hand.
  bell(name, r_throat, h_throat, r_exit, h_exit, wall=10, axis="z",
       centre=(0, 0), bulge=0.35)     nozzle bell shell from throat to exit
                                     (bulge 0 = straight cone)
cq (CadQuery) and math are available. Filter selections with bbox() when a
name pattern catches too much (for example only the parts below a joint).
A replacement must occupy about the same space as what it replaces: take
radii and heights from the bboxes of the parts you remove. Keep joints connected: when you stretch or move one section, move what is
attached to its free end with it. In "say", name the part or parts you
changed in plain words ("the inlet manifold ring at the nozzle exit"), so
the user can tell at once if it was the wrong one. If more than one part
fits the description, ask which. Return
  {"say": "...", "script": "<python>"}
""".strip()


VERIFY = """
You are looking at renders of a CAD model from a few angles, taken just after
an edit was applied. Judge ONLY whether the edit did what was asked without
breaking the model. Reply with ONE JSON object, no prose, no code fence:
  {"wrong": false}                                     it looks right
  {"wrong": true, "say": "<what is wrong, one sentence>"}

Say it is wrong only for something you can SEE: a part detached or floating,
a section obviously the wrong size relative to the rest, geometry turned
inside out, a hole or feature in the wrong place, parts intersecting that
should not, a shape that has exploded into spikes or shards. Faceting, render
quality, lighting, background and colour are not faults. If the change is
simply too subtle to see, that is not wrong — say false. You are the last
check before the user is told it worked, so do not invent problems, and do
not pass something visibly broken.
""".strip()


def main() -> int:
    req = json.load(sys.stdin)
    mode = req.get("mode")
    if mode == "verify":
        images = [p for p in (req.get("images") or []) if os.path.exists(p)]
        if not images:
            print(json.dumps({"wrong": False}))
            return 0
        listed = "\n".join(f"  {os.path.basename(p)}" for p in images)
        prompt = (f"Change that was requested: {req.get('request')}\n"
                  f"What the editor said it did: {req.get('said')}\n\n"
                  f"Read these renders and judge the result:\n{listed}")
        # Read, and the renders copied into the sandbox rather than reached
        # for where they live. This is the only place in the system where the
        # model is given a tool at all, so it gets the narrowest version of
        # it: one directory, containing nothing but the pictures.
        return _run(prompt, VERIFY, tools="Read", fallback={"wrong": False},
                    attach=images)

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

    return _run(prompt, system)


def _run(prompt: str, system: str, tools: str = "",
         fallback: dict = None, attach: list = None) -> int:
    """One isolated call, and print whatever comes back.

    `fallback` is what to print when the call cannot be made or times out.
    Verification passes {"wrong": false} there on purpose: a check that
    cannot run must not become a check that fails everything.
    """
    with tempfile.TemporaryDirectory(prefix="jarvis-edit-") as sandbox:
        for src in attach or []:
            try:
                shutil.copyfile(src, os.path.join(sandbox, os.path.basename(src)))
            except OSError:
                pass
        try:
            proc = subprocess.run(
                [find_binary(), "-p", prompt, "--system-prompt", system,
                 "--output-format", "text", "--model", MODEL,
                 "--setting-sources", "", "--strict-mcp-config",
                 "--allowed-tools", tools],
                capture_output=True, text=True, timeout=TIMEOUT, cwd=sandbox)
        except subprocess.TimeoutExpired:
            print(json.dumps(fallback or {"say": "That edit took too long; I stopped.",
                                          "failed": True}))
            return 0
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        print(json.dumps(fallback or {"say": "My link to Claude Code failed.", "failed": True,
                                      "error": tail[-1] if tail else ""}))
        return 0
    out = extract_json(proc.stdout)
    if not isinstance(out, dict):
        out = fallback or {"say": str(out)[:300]}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
