"""Finding and serving 3D models for the in-HUD viewer.

OpenSCAD is not required and does not have to be installed: the HUD renders
STL directly, and the hand landmarks that already stream over the websocket
turn it. That also means rotating a model needs no Accessibility permission,
because nothing is synthesising mouse events — the model is inside the page.

Every path is resolved against an allow-list of roots, so a stray request can
only ever reach a model directory.
"""
from __future__ import annotations

import pathlib
import re
import time
from typing import Dict, List, Optional

# Only what the HUD viewer can actually parse. Listing .3mf and .obj here
# indexed files the viewer then failed to open — an index that offers a model
# it cannot render is worse than one that omits it.
# What the HUD viewer can put on screen. .scad is here because OpenSCAD's
# command line compiles one to STL in well under a second, which means the
# model you turn is the live source rather than a stale export — and none of it
# needs Accessibility, because the model is inside the interface.
SUFFIXES = (".stl", ".scad", ".step", ".stp")
OPENSCAD = "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"

# STEP is what Onshape exports, and most of the Mark IV lives only in it. It
# is converted to STL by CadQuery, which is not in this environment; the first
# interpreter below that can import it is used, and the result is cached.
STEP_PYTHONS = ("~/FTC_BIOBUZZ/cqenv/bin/python", "python3")

# Source files cannot be rendered in the HUD — they are code that has to be
# compiled — but they can be opened in the application that owns them.
SOURCE_SUFFIXES = (".scad", ".f3d", ".sldprt", ".3mf")
SOURCE_APPS = {".scad": "OpenSCAD", ".f3d": "Autodesk Fusion 360",
               ".sldprt": "SOLIDWORKS", ".3mf": "OpenSCAD"}

# Library code is not a model of anything; never offer it as one.
_LIBRARY = re.compile(r"/(lib|libraries|BOSL2|MCAD|node_modules|\.git)/", re.I)

# Words that say nothing about which model this is, dropped when matching a
# spoken name against a filename. The format words matter: "open astrowilly
# scad" used to fail because "scad" had to land on a token, and it never can —
# it is the extension, which was stripped before the name was ever indexed.
_NOISE = re.compile(
    r"\b(the|a|an|file|please|my|show|open|load|view|display|bring|pull|up|"
    r"me|for|in|3d|model|part|assembly|drawing|scad|stl|step|stp|obj|cad)\b")


# Words that name part of the interface, not part of a machine. Several are
# also real filenames — there is a glass_window.stl — so "show the window"
# would otherwise put a part on screen instead of doing what was asked.
_UI_NOUNS = {"window", "windows", "tab", "tabs", "desktop", "screen", "page",
             "app", "apps", "menu", "folder", "space", "spaces"}


# What a spoken format word asks for, best first. "Open astrowilly cad" means
# the thing that was designed, not its export: the .scad compiles for the
# viewer and brings its dimension panel with it, where the exported .stl of the
# same name is a dead mesh. Without a format word the renderable STL still wins.
PREFER = {
    "source": (".scad", ".step", ".stp"),
    "step": (".step", ".stp"),
}


def _native(model: Dict[str, object], prefer: Optional[str]) -> int:
    suffix = str(model.get("suffix", ""))
    order = PREFER.get(prefer or "")
    if order:
        return len(order) - order.index(suffix) if suffix in order else 0
    return 2 if suffix == ".stl" else 1


def _squash(text: str) -> str:
    """A name with everything but letters and digits removed, for loose matching."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


class ModelIndex:
    def __init__(self, roots: List[str], max_files: int = 4000):
        self.roots = [pathlib.Path(r).expanduser().resolve() for r in roots]
        self.roots = [r for r in self.roots if r.is_dir()]
        self.max_files = max_files
        self._cache: List[Dict[str, object]] = []
        self._scanned = 0.0
        self._sources: List[Dict[str, object]] = []
        self._sources_scanned = 0.0

    # ----------------------------------------------------------- scanning

    def scan(self, force: bool = False, sources: bool = False) -> List[Dict[str, object]]:
        cache = self._sources if sources else self._cache
        stamp = self._sources_scanned if sources else self._scanned
        if cache and not force and time.time() - stamp < 120:
            return cache
        wanted = SOURCE_SUFFIXES if sources else SUFFIXES
        found: List[Dict[str, object]] = []
        for root in self.roots:
            for path in root.rglob("*"):
                if len(found) >= self.max_files:
                    break
                if path.suffix.lower() not in wanted or not path.is_file():
                    continue
                if _LIBRARY.search(str(path)):
                    continue
                if any(part.startswith(".") for part in path.parts):
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                found.append({
                    "name": path.stem,
                    "path": str(path),
                    "rel": str(path.relative_to(root)),
                    "project": root.name,
                    "size": size,
                    "suffix": path.suffix.lower(),
                    "app": SOURCE_APPS.get(path.suffix.lower()),
                })
        found.sort(key=lambda m: (str(m["project"]), str(m["name"])))
        if sources:
            self._sources, self._sources_scanned = found, time.time()
        else:
            self._cache, self._scanned = found, time.time()
        return found

    # ------------------------------------------------------------ lookup

    def renderable(self, raw: str, fine: bool = False) -> Optional[pathlib.Path]:
        """A path the viewer can load: STL as-is, .scad compiled and cached.
        fine=True converts a STEP at finer detail (for "make it smooth")."""
        path = self.resolve(raw)
        if path is None:
            return None
        suffix = path.suffix.lower()
        if suffix == ".scad":
            return self.compile_scad(path)
        if suffix in (".step", ".stp"):
            return self.compile_step(path, fine=fine)
        return path

    def _built(self, path: pathlib.Path, fine: bool = False) -> Optional[pathlib.Path]:
        """Where the viewer's STL for a source lives, keyed on its modification
        time; None for a file that is shown as it is."""
        import hashlib

        suffix = path.suffix.lower()
        if suffix == ".scad":
            folder, tail = "scad", ""
        elif suffix in (".step", ".stp"):
            folder, tail = "step", "-fine" if fine else ""
        else:
            return None
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return None
        key = hashlib.sha1(f"{path}:{stamp}".encode()).hexdigest()[:16]
        cache = pathlib.Path(__file__).resolve().parent.parent / "build" / folder
        cache.mkdir(parents=True, exist_ok=True)
        return cache / f"{path.stem}-{key}{tail}.stl"

    def needs_build(self, raw: str) -> bool:
        """True when showing this model means compiling it first. Astrowilly
        takes ~37 s cold, and a model that goes quiet that long after being
        announced reads as one that did not open."""
        path = self.resolve(raw)
        out = self._built(path) if path is not None else None
        return out is not None and not (out.exists() and out.stat().st_size > 0)

    def compile_scad(self, path: pathlib.Path) -> Optional[pathlib.Path]:
        """Compile a .scad to STL, cached on the source's modification time."""
        import subprocess

        if not pathlib.Path(OPENSCAD).exists():
            return None
        out = self._built(path)
        if out is None:
            return None
        if out.exists() and out.stat().st_size > 0:
            return out
        try:
            proc = subprocess.run(
                # binstl, not the default ASCII: 439 KB instead of 2.6 MB for
                # the same 8,780 triangles, and it parses without a text pass.
                # Manifold: 0.16 s against 5.9 s on CGAL for the glasses frame;
                # the MITE never finished on CGAL in ten minutes, 8 s here.
                [OPENSCAD, "--backend", "Manifold", "--export-format", "binstl",
                 "-o", str(out), str(path)],
                capture_output=True, text=True, timeout=180,
                cwd=str(path.parent),          # so its include<> paths resolve
            )
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)
            self.last_error = (proc.stderr or proc.stdout or "compile failed").strip()[-400:]
            return None
        return out

    _step_python: Optional[str] = None

    def step_python(self) -> Optional[str]:
        """The first interpreter that can import CadQuery, found once."""
        import shutil
        import subprocess
        if self._step_python:
            return self._step_python
        for cand in STEP_PYTHONS:
            exe = str(pathlib.Path(cand).expanduser()) if "/" in cand else shutil.which(cand)
            if not exe or not pathlib.Path(exe).exists():
                continue
            try:
                ok = subprocess.run([exe, "-c", "import cadquery"], capture_output=True,
                                    timeout=60).returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                ok = False
            if ok:
                self._step_python = exe
                return exe
        return None

    # Facet angle is what makes a converted nozzle look like strips: 0.45 rad
    # (26 degrees) is quick, 0.25 rad (14 degrees) is 2.5 times the triangles
    # (the aft engine: 337k -> 828k, 4.7 s -> 5.9 s). 0.15 rad was 3.3 M and
    # 166 MB, too heavy to turn by hand.
    STEP_COARSE = ("0.8", "0.45")
    STEP_FINE = ("0.8", "0.25")

    def compile_step(self, path: pathlib.Path, fine: bool = False) -> Optional[pathlib.Path]:
        """Convert a STEP file to binary STL, cached on its modification time."""
        import subprocess

        exe = self.step_python()
        if exe is None:
            self.last_error = "no Python with CadQuery to convert STEP"
            return None
        out = self._built(path, fine=fine)
        if out is None:
            return None
        if out.exists() and out.stat().st_size > 0:
            return out
        script = pathlib.Path(__file__).resolve().parent / "step2stl.py"
        tol, ang = self.STEP_FINE if fine else self.STEP_COARSE
        try:
            proc = subprocess.run([exe, str(script), str(path), str(out), tol, ang],
                                  capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            self.last_error = "STEP conversion timed out"
            return None
        if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
            out.unlink(missing_ok=True)
            self.last_error = (proc.stderr or proc.stdout or "conversion failed").strip()[-400:]
            return None
        return out

    def resolve(self, raw: str) -> Optional[pathlib.Path]:
        """Turn a path string into a real file, but only inside a known root."""
        try:
            path = pathlib.Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
        if path.suffix.lower() not in SUFFIXES or not path.is_file():
            return None
        for root in self.roots:
            try:
                path.relative_to(root)
                return path
            except ValueError:
                continue
        return None

    def search(self, query: str, limit: int = 12, sources: bool = False,
               prefer: Optional[str] = None) -> List[Dict[str, object]]:
        """Rank models against a spoken or typed name."""
        words = [w for w in _NOISE.sub(" ", query.lower()).split() if len(w) > 1]
        if not words:
            return []
        # A name made only of interface words is not a model name, however well
        # it happens to match a filename.
        if all(w in _UI_NOUNS for w in words):
            return []
        scored = []
        for model in self.scan(sources=sources):
            name = str(model["name"]).lower().replace("_", " ").replace("-", " ")
            project = str(model["project"]).lower().replace("_", " ")
            tokens = set(name.split()) | set(project.split())
            # Whole tokens only. Substring matching made "all" match "small",
            # so "show me all my windows" resolved to a model.
            hits = sum(1 for w in words
                       if w in tokens or any(t.startswith(w) and len(w) >= 4
                                             for t in tokens))
            # Every word must land, or this is not the model being asked for.
            if hits < len(words):
                continue
            # Prefer the whole phrase, then a shorter name — "mite" should beat
            # "mite_packs_v3_old".
            exact = 2 if " ".join(words) in name else 0
            native = _native(model, prefer)
            scored.append((exact, native, hits, -len(name), model))
        if not scored:
            scored = self._fuzzy(words, sources=sources, prefer=prefer)
        scored.sort(key=lambda s: s[:4], reverse=True)
        return [s[4] for s in scored[:limit]]

    def _fuzzy(self, words: List[str], sources: bool = False,
               prefer: Optional[str] = None) -> List[tuple]:
        """Second pass for a name that was not said exactly.

        Only reached when nothing matched strictly, and deliberately narrow:
        run together, the spoken name has to be most of the filename. That
        finds "astrowilly" from "astro willy" and survives a mistranscribed
        syllable, while "the desktop" still finds nothing — which is what lets
        the model rules decline an utterance that is not about a model at all.
        """
        import difflib

        said = _squash(" ".join(words))
        if len(said) < 5:
            return []
        scored: List[tuple] = []
        for model in self.scan(sources=sources):
            name = _squash(str(model["name"]))
            if not name:
                continue
            if said in name or name in said:
                ratio = 0.95
            else:
                ratio = difflib.SequenceMatcher(None, said, name).ratio()
            if ratio < 0.75:
                continue
            native = _native(model, prefer)
            # Bucketed, because at this point every survivor is already a
            # plausible reading of what was said and the third decimal is
            # noise. A longer filename can out-score the thing it is a variant
            # of — "astro willie" scores astrowilly_site above astrowilly —
            # so a near-tie is settled by the tiebreakers instead: prefer the
            # format asked for (the renderable STL if none), then the shortest name.
            scored.append((0, round(ratio, 1), native, -len(name), model))
        return scored

    def best(self, query: str, prefer: Optional[str] = None) -> Optional[Dict[str, object]]:
        hits = self.search(query, limit=1, prefer=prefer)
        return hits[0] if hits else None

    def best_source(self, query: str) -> Optional[Dict[str, object]]:
        """The source file for a name, for opening in its own application."""
        hits = self.search(query, limit=1, sources=True)
        return hits[0] if hits else None
