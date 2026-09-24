"""Live parameter editing for OpenSCAD models shown in the HUD.

An OpenSCAD part is a program whose top-level assignments are its dimensions:
`rim_t = 2.4;`. Editing one does not need a CAD application. JARVIS recompiles
with `-D name=value`, which OpenSCAD appends after the file's own assignments
so the override wins, including for values that came from an `include`d
params file. The source file is untouched until "save", which rewrites only
the numbers on those lines and keeps a timestamped copy of the original.

Compiles use the Manifold backend: 0.16 s for the glasses frame against 5.9 s
on CGAL, about a second for most parts, 8 s for the whole MITE. Only the latest
request matters while a slider or a hand is moving, so a new compile kills the
one in flight.
"""
from __future__ import annotations

import difflib
import hashlib
import pathlib
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import children

ROOT = pathlib.Path(__file__).resolve().parent.parent
CACHE = ROOT / "build" / "scad_edit"
BACKUPS = ROOT / "build" / "scad_backups"

# `name = 12.5;  // comment [0:0.5:40]` at brace depth 0. Only literal numbers
# and booleans are parameters; `a = b * 2;` is derived and follows its inputs.
_ASSIGN = re.compile(
    r"^(?P<indent>\s*)(?P<name>[A-Za-z_]\w*)(?P<eq>\s*=\s*)"
    r"(?P<value>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|true|false)"
    r"(?P<tail>\s*;.*)$")
# OpenSCAD Customizer ranges: [max], [min:max] or [min:step:max].
_RANGE = re.compile(r"\[\s*(-?[\d.]+)\s*(?::\s*(-?[\d.]+)\s*)?(?::\s*(-?[\d.]+)\s*)?\]")


@dataclass
class Param:
    name: str
    value: object                 # float, int or bool as written in the file
    kind: str                     # "number", "int" or "bool"
    line: int                     # 0-based line in the source
    comment: str = ""
    lo: Optional[float] = None
    hi: Optional[float] = None
    step: Optional[float] = None

    @property
    def spoken(self) -> str:
        """How a person says the name: rim_t -> "rim t", panelGap -> "panel gap"."""
        s = re.sub(r"([a-z])([A-Z])", r"\1 \2", self.name)
        return re.sub(r"[_\s]+", " ", s).strip().lower()

    @property
    def readable(self) -> str:
        """For saying aloud: lens_w -> "lens width", ear_bend_deg -> "ear bend angle"."""
        return " ".join(_READ.get(w, w) for w in self.spoken.split())

    def to_dict(self, current: object) -> Dict[str, object]:
        lo, hi, step = self.bounds()
        return {"name": self.name, "spoken": self.readable, "kind": self.kind,
                "value": current, "base": self.value, "comment": self.comment,
                "lo": lo, "hi": hi, "step": step}

    def bounds(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """Slider range: the Customizer's if the file gives one, otherwise zero
        to three times the written value (a count steps by one)."""
        if self.kind == "bool":
            return None, None, None
        if self.lo is not None and self.hi is not None:
            return self.lo, self.hi, self.step
        v = float(self.value)
        if v == 0:
            return -10.0, 10.0, 1.0 if self.kind == "int" else 0.1
        lo, hi = (0.0, v * 3) if v > 0 else (v * 3, 0.0)
        step = 1.0 if self.kind == "int" else _nice_step(abs(v) / 50)
        return lo, hi, step


# How dimensions are abbreviated in variable names, read back as words.
_ABBREV = {
    "t": ("thickness", "thick", "wall"), "th": ("thickness", "thick"),
    "w": ("width", "wide"), "wd": ("width",), "h": ("height", "high", "tall"),
    "l": ("length", "long"), "len": ("length", "long"), "lg": ("length",),
    "r": ("radius", "round", "corner"), "rad": ("radius",),
    "d": ("diameter", "dia"), "dia": ("diameter",), "od": ("outer", "outside", "diameter"),
    "id": ("inner", "inside", "diameter"), "deg": ("angle", "degrees", "degree"),
    "ang": ("angle",), "a": ("angle",), "n": ("count", "number"), "num": ("count", "number"),
    "clr": ("clearance", "gap"), "gap": ("clearance", "spacing"), "pcd": ("pitch", "circle"),
    "x": ("x",), "y": ("y",), "z": ("z", "height"), "pos": ("position",), "off": ("offset",),
    "ipd": ("pupil", "pupillary", "interpupillary", "eye", "distance"),
}
_READ = {"t": "thickness", "th": "thickness", "w": "width", "h": "height", "l": "length",
         "len": "length", "r": "radius", "rad": "radius", "d": "diameter", "dia": "diameter",
         "od": "outer diameter", "id": "inner diameter", "deg": "angle", "ang": "angle",
         "n": "count", "num": "count", "clr": "clearance", "ipd": "I P D", "pcd": "bolt circle",
         "off": "offset", "pos": "position"}
_FILLER = {"of", "the", "a", "an", "and", "its", "it", "on", "for", "to"}


def _singular(w: str) -> str:
    if len(w) > 4 and w.endswith("es") and w[-3] in "sxz":
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss") and w[:-1] not in _ABBREV:
        return w[:-1]                 # but "lens" is not the plural of "len"
    return w


def _same(a: str, b: str) -> bool:
    """Equal, or one is the start of the other and both are real words:
    "pantoscopic" and "panto", "temple" and "temples". Never on one letter."""
    if a == b:
        return True
    if min(len(a), len(b)) < 4:       # "len" is not the start of "lens"
        return False
    return a.startswith(b) or b.startswith(a)


def _nice_step(x: float) -> float:
    if x <= 0:
        return 0.1
    exp = 10 ** int(f"{x:e}".split("e")[1])
    for m in (1, 2, 5, 10):
        if x <= m * exp:
            return m * exp
    return 10 * exp


# Whole numbers are usually millimetres written without a decimal point
# (lens_w = 50). Only a name that reads as a count steps by one and rounds.
_COUNT = re.compile(r"(?:^n_|_n$|^n[A-Z]|^N_|_N$|count|num|teeth|holes|segs?\b|segments|_fn$|^fn$|rows|cols|nbolt|_n_)",
                    re.I)


def _literal(text: str, name: str = "") -> Tuple[object, str]:
    if text in ("true", "false"):
        return text == "true", "bool"
    if re.fullmatch(r"-?\d+", text) and _COUNT.search(name):
        return int(text), "int"
    return float(text), "number"


def parameters(path: pathlib.Path) -> List[Param]:
    """The file's own top-level literal assignments, in order."""
    return parameters_text(path.read_text(errors="replace"))


def parameters_text(text: str) -> List[Param]:
    out: List[Param] = []
    depth = 0
    seen: Dict[str, int] = {}
    for i, line in enumerate(text.splitlines()):
        code = line.split("//", 1)[0]
        if depth == 0:
            m = _ASSIGN.match(line)
            if m:
                value, kind = _literal(m.group("value"), m.group("name"))
                tail = m.group("tail")
                comment = tail.split("//", 1)[1].strip() if "//" in tail else ""
                p = Param(m.group("name"), value, kind, i, comment)
                r = _RANGE.search(comment)
                if r and kind != "bool":
                    a, b, c = (float(x) if x is not None else None for x in r.groups())
                    if b is None:                    # [max]
                        p.lo, p.hi = 0.0, a
                    elif c is None:                  # [min:max]
                        p.lo, p.hi = a, b
                    else:                            # [min:step:max]
                        p.lo, p.step, p.hi = a, b, c
                    comment = _RANGE.sub("", comment).strip()
                    p.comment = comment
                # A later top-level assignment wins in OpenSCAD; keep that one.
                if p.name in seen:
                    out[seen[p.name]] = p
                else:
                    seen[p.name] = len(out)
                    out.append(p)
        depth += code.count("{") - code.count("}")
        depth = max(depth, 0)
    return out


def _format(value: object, kind: str) -> str:
    if kind == "bool":
        return "true" if value else "false"
    if kind == "int":
        return str(int(round(float(value))))
    v = float(value)
    text = f"{v:.6g}"
    return text                      # 50 stays 50, 3.5 stays 3.5


class EditSession:
    """One .scad file being edited. Overrides live here until saved."""

    def __init__(self, path: pathlib.Path, openscad: str):
        self.path = path
        self.openscad = openscad
        self.params = parameters(path)
        self.by_name = {p.name: p for p in self.params}
        self.overrides: Dict[str, object] = {}
        # Source edited by "add a hole" and the like: held here, not on disk,
        # until "save". None means the file as it is.
        self.text: Optional[str] = None
        self.history: List[Dict[str, object]] = []   # snapshots {"o": overrides, "t": text}
        self.last_error = ""
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._generation = 0
        self._last_edit: Tuple[str, float] = ("", 0.0)

    # ------------------------------------------------------------ lookup

    def candidates(self, spoken: str, among: Optional[List["Param"]] = None) -> List[Tuple["Param", float]]:
        """Parameters a spoken name could mean, best first, with scores.
        An exact name scores 2. CAD abbreviations are read as words, so
        "rim thickness" finds rim_t and "ear bend angle" finds ear_bend_deg."""
        want = re.sub(r"[_\s]+", " ", spoken.lower()).strip()
        want = re.sub(r"^(?:the|a|an|its|that|this)\s+", "", want)
        pool = among if among is not None else self.params
        if not want or not pool:
            return []
        squash = want.replace(" ", "")
        qwords = [_singular(w) for w in want.split() if w not in _FILLER]
        scored = []
        for p in pool:
            if p.spoken == want or p.name.lower() == squash:
                scored.append((p, 2.0))
                continue
            pwords = p.spoken.split()
            vocab = set(pwords) | {x for w in pwords for x in _ABBREV.get(w, ())}
            hits = sum(1 for q in qwords if any(_same(q, v) for v in vocab))
            if not qwords:
                continue
            covered = sum(1 for w in pwords if any(_same(q, w) or q in _ABBREV.get(w, ())
                                                    for q in qwords))
            score = (hits / len(qwords)) * (0.75 + 0.25 * covered / len(pwords))
            # a mishearing still lands: "rim thickniss", "panto tilt"
            score = max(score, difflib.SequenceMatcher(None, want, p.spoken).ratio() * 0.8)
            if score >= 0.5:
                scored.append((p, score))
        scored.sort(key=lambda t: -t[1])
        return scored

    def resolve(self, spoken: str, among: Optional[List["Param"]] = None
                ) -> Tuple[Optional["Param"], List["Param"]]:
        """(the parameter, []) when the name is clear; (None, options) when it
        could be several ("the lenses": width, height or radius); (None, [])
        when nothing is close."""
        ranked = self.candidates(spoken, among)
        if not ranked:
            return None, []
        top = ranked[0][1]
        close = [p for p, s in ranked if top - s < 0.03]
        if top >= 2.0 or len(close) == 1:
            return ranked[0][0], []
        return None, close[:4]

    def find(self, spoken: str) -> Optional[Param]:
        """The parameter only when the name is unambiguous."""
        return self.resolve(spoken)[0]

    def current(self, name: str) -> object:
        return self.overrides.get(name, self.by_name[name].value)

    def state(self) -> List[Dict[str, object]]:
        return [p.to_dict(self.current(p.name)) for p in self.params]

    # ------------------------------------------------------------ edits

    def set(self, name: str, value: object) -> object:
        p = self.by_name[name]
        if p.kind == "bool":
            value = bool(value)
        elif p.kind == "int":
            value = int(round(float(value)))
        else:
            value = float(f"{float(value):.6g}")      # 55.00000000000001 -> 55.0
        if value == self.current(name):
            return value
        # A drag sends a stream of values for one dimension: that is one edit,
        # so "undo" takes the whole drag back, not its last tick.
        last_name, last_t = self._last_edit
        now = time.monotonic()
        if not (last_name == name and now - last_t < 1.5):
            self.history.append(self._snapshot())
        self._last_edit = (name, now)
        if value == p.value:
            self.overrides.pop(name, None)
        else:
            self.overrides[name] = value
        return value

    def nudge(self, name: str, up: bool, amount: Optional[float] = None,
              percent: bool = False) -> object:
        """Bigger or smaller: by a stated amount, a stated percentage, or 10%
        (one step for a count). Zero cannot be scaled, so it steps."""
        p = self.by_name[name]
        cur = self.current(name)
        if p.kind == "bool":
            return self.set(name, up)
        sign = 1 if up else -1
        cur = float(cur)
        if amount is not None and not percent:
            new = cur + sign * amount
        elif p.kind == "int" and amount is None:
            new = cur + sign
        else:
            frac = (amount if amount is not None else 10.0) / 100.0
            new = cur * (1 + sign * frac) if cur != 0 else sign * (p.bounds()[2] or 1.0)
        return self.set(name, new)

    def _snapshot(self) -> Dict[str, object]:
        return {"o": dict(self.overrides), "t": self.text}

    def _restore(self, snap: Dict[str, object]) -> None:
        text = snap["t"]
        if text != self.text:
            self._reparse(text)
        self.overrides = dict(snap["o"])

    def _reparse(self, text: Optional[str]) -> None:
        self.text = text
        self.params = parameters_text(text if text is not None else self.path.read_text(errors="replace"))
        self.by_name = {p.name: p for p in self.params}
        self.overrides = {k: v for k, v in self.overrides.items() if k in self.by_name}

    @property
    def dirty(self) -> bool:
        return bool(self.overrides) or self.text is not None

    def source(self) -> str:
        return self.text if self.text is not None else self.path.read_text(errors="replace")

    def set_source(self, text: str) -> None:
        """A whole new source, from an edit to the code itself."""
        self.history.append(self._snapshot())
        self._last_edit = ("", 0.0)
        self._reparse(text)

    def undo(self) -> bool:
        if not self.history:
            return False
        self._last_edit = ("", 0.0)
        self._restore(self.history.pop())
        return True

    def reset(self) -> None:
        if self.dirty:
            self.history.append(self._snapshot())
        self.overrides = {}
        if self.text is not None:
            self._reparse(None)

    # ------------------------------------------------------------ build

    def key(self) -> str:
        stamp = self.path.stat().st_mtime_ns
        src = hashlib.sha1(self.text.encode()).hexdigest() if self.text is not None else "file"
        body = f"{self.path}:{stamp}:{src}:" + ";".join(
            f"{k}={_format(v, self.by_name[k].kind)}" for k, v in sorted(self.overrides.items()))
        return hashlib.sha1(body.encode()).hexdigest()[:16]

    def output(self, key: Optional[str] = None) -> pathlib.Path:
        return CACHE / f"{self.path.stem}-{key or self.key()}.stl"

    def compile(self) -> Optional[pathlib.Path]:
        """Build the current overrides. A newer call kills this one; the loser
        returns None and its caller simply drops the result."""
        CACHE.mkdir(parents=True, exist_ok=True)
        self.last_error = ""
        key = self.key()
        out = self.output(key)
        if out.exists() and out.stat().st_size > 0:
            return out
        args = [self.openscad, "--backend", "Manifold", "--export-format", "binstl",
                "-o", str(out)]
        for k, v in sorted(self.overrides.items()):
            args += ["-D", f"{k}={_format(v, self.by_name[k].kind)}"]
        # Edited source compiles from a hidden file beside the original, so its
        # include<> and use<> paths still resolve; it is removed right after.
        scratch = None
        if self.text is not None:
            scratch = self.path.with_name(f".jarvis-edit-{self.path.stem}.scad")
            scratch.write_text(self.text)
        args.append(str(scratch or self.path))
        try:
            return self._compile(args, out)
        finally:
            if scratch is not None:
                scratch.unlink(missing_ok=True)

    def _compile(self, args: List[str], out: pathlib.Path) -> Optional[pathlib.Path]:
        with self._lock:
            self._generation += 1
            gen = self._generation
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
            proc = children.popen(args, cwd=str(self.path.parent),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self._proc = proc
        try:
            _, err = proc.communicate(timeout=240)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.last_error = "compile timed out"
            out.unlink(missing_ok=True)
            return None
        finally:
            children.release(proc)
        if gen != self._generation:          # superseded while running
            out.unlink(missing_ok=True)
            return None
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)
            self.last_error = (err or "compile failed").strip()[-400:]
            return None
        return out

    # ------------------------------------------------------------ save

    def save(self) -> Optional[pathlib.Path]:
        """Write the edits into the source: edited code as it stands, and each
        changed dimension as only the number on its line. The original is
        copied to build/scad_backups first. Returns the backup."""
        if not self.dirty:
            return None
        BACKUPS.mkdir(parents=True, exist_ok=True)
        backup = BACKUPS / f"{self.path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.scad"
        shutil.copy2(self.path, backup)
        lines = self.source().splitlines(keepends=True)
        for name, value in self.overrides.items():
            p = self.by_name[name]
            raw = lines[p.line]
            end = "\n" if raw.endswith("\n") else ""
            m = _ASSIGN.match(raw.rstrip("\n"))
            if not m or m.group("name") != name:
                raise RuntimeError(f"{self.path.name} changed on disk at {name}; not saved")
            lines[p.line] = (m.group("indent") + name + m.group("eq") +
                             _format(value, p.kind) + m.group("tail") + end)
        self.path.write_text("".join(lines))
        # The written file is now the base.
        self.overrides = {}
        self._reparse(None)
        self.history = []
        return backup
