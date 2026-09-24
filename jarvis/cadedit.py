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

    def to_dict(self, current: object) -> Dict[str, object]:
        lo, hi, step = self.bounds()
        return {"name": self.name, "spoken": self.spoken, "kind": self.kind,
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
    out: List[Param] = []
    depth = 0
    seen: Dict[str, int] = {}
    for i, line in enumerate(path.read_text(errors="replace").splitlines()):
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
        self.history: List[Dict[str, object]] = []
        self.last_error = ""
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._generation = 0
        self._last_edit: Tuple[str, float] = ("", 0.0)

    # ------------------------------------------------------------ lookup

    def find(self, spoken: str) -> Optional[Param]:
        """Match a spoken name to a parameter: exact, then word overlap, then
        closest spelling ("rim thickness" finds rim_t, "wall" finds wall_t)."""
        want = re.sub(r"[_\s]+", " ", spoken.lower()).strip()
        want = re.sub(r"^(?:the|a)\s+", "", want)
        if not want or not self.params:
            return None
        squash = want.replace(" ", "")
        for p in self.params:
            if p.spoken == want or p.name.lower() == squash:
                return p
        words = set(want.split())
        best, score = None, 0.0
        for p in self.params:
            pw = set(p.spoken.split())
            # "rim thickness" vs "rim t": a word may be a prefix of the other.
            hits = sum(1 for w in words if any(x == w or (len(x) >= 1 and w.startswith(x)) or
                                               (len(w) >= 3 and x.startswith(w)) for x in pw))
            s = hits / max(len(words), len(pw))
            s = max(s, difflib.SequenceMatcher(None, want, p.spoken).ratio() * 0.9)
            if s > score:
                best, score = p, s
        return best if score >= 0.5 else None

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
            value = float(value)
        if value == self.current(name):
            return value
        # A drag sends a stream of values for one dimension: that is one edit,
        # so "undo" takes the whole drag back, not its last tick.
        last_name, last_t = self._last_edit
        now = time.monotonic()
        if not (last_name == name and now - last_t < 1.5):
            self.history.append(dict(self.overrides))
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

    def undo(self) -> bool:
        if not self.history:
            return False
        self._last_edit = ("", 0.0)
        self.overrides = self.history.pop()
        return True

    def reset(self) -> None:
        if self.overrides:
            self.history.append(dict(self.overrides))
        self.overrides = {}

    # ------------------------------------------------------------ build

    def key(self) -> str:
        stamp = self.path.stat().st_mtime_ns
        body = f"{self.path}:{stamp}:" + ";".join(
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
        args.append(str(self.path))
        with self._lock:
            self._generation += 1
            gen = self._generation
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
            proc = subprocess.Popen(args, cwd=str(self.path.parent),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self._proc = proc
        try:
            _, err = proc.communicate(timeout=240)
        except subprocess.TimeoutExpired:
            proc.kill()
            self.last_error = "compile timed out"
            out.unlink(missing_ok=True)
            return None
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
        """Write the overrides into the source, only the number on each line.
        The original is copied to build/scad_backups first. Returns the backup."""
        if not self.overrides:
            return None
        BACKUPS.mkdir(parents=True, exist_ok=True)
        backup = BACKUPS / f"{self.path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.scad"
        shutil.copy2(self.path, backup)
        lines = self.path.read_text(errors="replace").splitlines(keepends=True)
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
        # The written values are now the base.
        self.params = parameters(self.path)
        self.by_name = {p.name: p for p in self.params}
        self.overrides = {}
        self.history = []
        return backup
