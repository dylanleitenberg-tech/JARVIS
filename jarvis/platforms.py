"""Where things are on this machine.

The only module that knows install locations: the browser the HUD opens in,
OpenSCAD, ollama, the Claude CLI, a Python with CadQuery. Everything is looked
for rather than assumed, because the same code runs on a Mac with Homebrew, a
Windows laptop with nothing but Edge, and a Linux box with Chromium from a
package manager, and "not installed" has to be a sentence, not a traceback.
"""
from __future__ import annotations

import glob
import os
import pathlib
import shutil
import sys
from typing import List, Optional, Tuple

MAC = sys.platform == "darwin"
WINDOWS = sys.platform == "win32"
LINUX = not (MAC or WINDOWS)

HOME = pathlib.Path.home()


def _first(paths) -> Optional[str]:
    for p in paths:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _win_dirs(*tail: str) -> List[str]:
    roots = [os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    return [os.path.join(r, *tail) for r in roots if r]


# --------------------------------------------------------------------- browser

def browser() -> Tuple[Optional[str], str, bool]:
    """(executable, name, can_recognise_speech) for the HUD window.

    Speech recognition in the page is the browser's own service: Google Chrome
    and Microsoft Edge ship it, Chromium and Brave do not (they lack Google's
    key, so recognition fails with "network"). A browser without it still runs
    the interface, with typed commands.
    """
    if MAC:
        cands = [("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "Google Chrome", True),
                 (str(HOME / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), "Google Chrome", True),
                 ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "Microsoft Edge", True),
                 ("/Applications/Chromium.app/Contents/MacOS/Chromium", "Chromium", False),
                 ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "Brave", False)]
    elif WINDOWS:
        cands = ([(p, "Google Chrome", True) for p in _win_dirs("Google", "Chrome", "Application", "chrome.exe")]
                 + [(p, "Microsoft Edge", True) for p in _win_dirs("Microsoft", "Edge", "Application", "msedge.exe")]
                 + [(p, "Brave", False) for p in _win_dirs("BraveSoftware", "Brave-Browser", "Application", "brave.exe")])
    else:
        cands = [(shutil.which(n), label, speech) for n, label, speech in (
            ("google-chrome", "Google Chrome", True), ("google-chrome-stable", "Google Chrome", True),
            ("microsoft-edge", "Microsoft Edge", True), ("microsoft-edge-stable", "Microsoft Edge", True),
            ("chromium", "Chromium", False), ("chromium-browser", "Chromium", False),
            ("brave-browser", "Brave", False))]
    for path, name, speech in cands:
        if path and os.path.isfile(path):
            return path, name, speech
    return None, "", False


# ------------------------------------------------------------------- OpenSCAD

def openscad() -> Optional[str]:
    if MAC:
        return _first(["/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
                       str(HOME / "Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"),
                       shutil.which("openscad")])
    if WINDOWS:
        return _first(_win_dirs("OpenSCAD", "openscad.exe") + [shutil.which("openscad")])
    return shutil.which("openscad") or _first(["/snap/bin/openscad"])


def app_executable(app: str) -> Optional[str]:
    """An application the user named, for opening a file in it."""
    key = app.lower().replace(" ", "")
    if key in ("openscad", "scad"):
        return openscad()
    return shutil.which(key)


# -------------------------------------------------------------------- ollama

def ollama() -> Optional[str]:
    return _first([shutil.which("ollama"),
                   "/usr/local/bin/ollama", "/opt/homebrew/bin/ollama",
                   "/Applications/Ollama.app/Contents/Resources/ollama",
                   os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
                   "/usr/bin/ollama"])


# ---------------------------------------------------------------- Claude CLI

def claude_cli() -> Optional[str]:
    """The Claude Code CLI, for someone who has installed it themselves.

    Used only when ai.backend or ai.smart_backend names claude-code: the CLI
    runs on its owner's own login, which is theirs to use, not something this
    program hands out.
    """
    exe = "claude.exe" if WINDOWS else "claude"
    found = shutil.which("claude")
    if found:
        return found
    pattern = str(HOME / ".vscode" / "extensions" / "anthropic.claude-code-*"
                  / "resources" / "native-binary" / exe)
    hits = sorted(glob.glob(pattern))
    return hits[-1] if hits else None


# ----------------------------------------------------------------- CadQuery

def step_pythons() -> List[str]:
    """Interpreters to try for STEP conversion, best first. CadQuery is a large
    optional install; `jarvis-setup --step` puts one in .step-env."""
    root = pathlib.Path(__file__).resolve().parent.parent
    local = root / ".step-env" / ("Scripts/python.exe" if WINDOWS else "bin/python")
    extra = os.environ.get("JARVIS_STEP_PYTHON", "")
    return [p for p in (extra, str(local), sys.executable) if p]


# ------------------------------------------------------------------ folders

def default_model_roots() -> List[str]:
    """Where people keep CAD files, if those folders exist here."""
    cands = [HOME / "Documents", HOME / "Desktop", HOME / "Downloads",
             HOME / "OneDrive" / "Documents"]
    return [str(p) for p in cands if p.is_dir()]
