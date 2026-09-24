"""Everything this needs from the machine, what each one's state is, and how
to ask for it — on whichever operating system this is.

The HUD shows these as the setup panel on load. On the first launch the
prompts the operating system owns (Accessibility and app control on a Mac)
are raised straight away, once each; after that they are a button in the
panel, so a refusal is respected rather than re-asked at every start. The
camera prompt comes from opening the camera and the microphone prompt from
the page, both of which happen on load anyway.

A row is: id, label, why it is needed, state, a detail sentence, and the
action its button performs. State is one of
  ok        granted / working
  waiting   not decided yet, or still starting
  needed    required and missing — the button fixes it
  limited   works partly, and the detail says what is missing
  optional  not required; the button adds it
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from . import config as config_module
from . import platforms
from .control import PLATFORM, desktop

STATE = config_module.ROOT / "logs" / "permissions.json"


def _load() -> Dict[str, Any]:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save(data: Dict[str, Any]) -> None:
    try:
        STATE.parent.mkdir(exist_ok=True)
        STATE.write_text(json.dumps(data, indent=1))
    except OSError:
        pass


def _row(id: str, label: str, why: str, state: str, detail: str,
         action: Optional[str] = None, required: bool = True) -> Dict[str, Any]:
    return {"id": id, "label": label, "why": why, "state": state,
            "detail": detail, "action": action, "required": required}


class Permissions:
    def __init__(self, host):
        self.host = host                     # the Jarvis instance
        self.saved = _load()
        self._pulling = None                 # an `ollama pull` in progress

    # ---------------------------------------------------------------- rows

    def rows(self) -> List[Dict[str, Any]]:
        rows = [self._camera(), self._microphone(), self._control()]
        if PLATFORM == "macos":
            rows.append(self._automation())
            rows.append(self._screen())
        rows.append(self._brain())
        rows.append(self._models())
        return rows

    def summary(self) -> Dict[str, Any]:
        rows = self.rows()
        missing = [r for r in rows if r["required"] and r["state"] in ("needed", "waiting")]
        return {"platform": PLATFORM, "rows": rows,
                "ready": not missing,
                "seen": bool(self.saved.get("setup_seen")),
                "restart_needed": bool(self.saved.get("restart_needed"))}

    def _camera(self) -> Dict[str, Any]:
        why = "see your hands, to point, pinch and turn models"
        host = self.host
        if not host.cfg["vision"]["enabled"]:
            return _row("camera", "Camera", why, "optional", "turned off", required=False)
        if host.vision and host.vision.running:
            return _row("camera", "Camera", why, "ok", "tracking")
        err = host.vision_error() if hasattr(host, "vision_error") else ""
        if err:
            return _row("camera", "Camera", why, "needed", err, action="open_settings")
        if host.vision is None:
            return _row("camera", "Camera", why, "waiting", "starting…")
        return _row("camera", "Camera", why, "waiting", "opening the camera…")

    def _microphone(self) -> Dict[str, Any]:
        why = "hear you say “Jarvis” and a command"
        mic = self.host.stat.get("mic", "unknown")
        _, browser, speech = platforms.browser()
        if mic == "live":
            if not speech:
                return _row("microphone", "Microphone", why, "limited",
                            f"{browser or 'this browser'} has no speech recognition — install "
                            "Google Chrome or Microsoft Edge for voice; typing works meanwhile",
                            action="get_browser")
            return _row("microphone", "Microphone", why, "ok", f"listening in {browser}")
        if mic == "unknown":
            return _row("microphone", "Microphone", why, "waiting",
                        "the interface asks as it opens — allow it there", action="ask_page")
        if mic == "unsupported browser":
            return _row("microphone", "Microphone", why, "needed",
                        "this browser cannot recognise speech — install Google Chrome "
                        "or Microsoft Edge", action="get_browser")
        detail = self.host.stat.get("mic_detail") or mic
        if mic == "macos-denied" or "denied" in detail.lower() or "refused" in detail.lower():
            detail = (f"refused — allow {browser or 'the browser'} in the microphone settings, "
                      f"then restart JARVIS")
        return _row("microphone", "Microphone", why, "needed", detail, action="open_settings")

    def _control(self) -> Dict[str, Any]:
        why = "move the mouse, press keys, arrange windows, drive CAD apps"
        trusted = desktop.accessibility_trusted() or desktop.relay_available()
        if PLATFORM == "macos":
            if trusted:
                return _row("control", "Accessibility", why, "ok", "granted")
            if self.saved.get("asked", {}).get("control"):
                detail = ("allowed in System Settings? Then restart JARVIS — a grant "
                          "never reaches a program that is already running")
                return _row("control", "Accessibility", why, "needed", detail, action="restart"
                            if self.saved.get("restart_needed") else "ask_os")
            return _row("control", "Accessibility", why, "needed",
                        "not granted yet", action="ask_os")
        if PLATFORM == "windows":
            return _row("control", "Keyboard & mouse", why, "ok",
                        "Windows needs no permission (except over admin windows)")
        if trusted:
            return _row("control", "Keyboard & mouse", why, "ok", "X11 session")
        wayland = getattr(desktop, "wayland", lambda: False)()
        return _row("control", "Keyboard & mouse", why, "limited",
                    "Wayland does not let apps type or click into each other; log in "
                    "with an X11 (Xorg) session for full control" if wayland else
                    "no X display found", required=False)

    def _automation(self) -> Dict[str, Any]:
        why = "open, quit and arrange other apps"
        state = self.saved.get("automation")
        if state == "ok":
            return _row("automation", "App control", why, "ok", "granted")
        if state == "denied":
            return _row("automation", "App control", why, "needed",
                        "refused — turn on JARVIS › System Events in Automation settings",
                        action="open_settings")
        return _row("automation", "App control", why, "waiting", "not asked yet",
                    action="ask_os")

    def _screen(self) -> Dict[str, Any]:
        why = "screenshots of other apps’ windows"
        try:
            import Quartz
            granted = bool(Quartz.CGPreflightScreenCaptureAccess())
        except Exception:
            granted = False
        if granted:
            return _row("screen", "Screen recording", why, "ok", "granted", required=False)
        return _row("screen", "Screen recording", why, "optional",
                    "only needed for screenshots", action="ask_os", required=False)

    def _brain(self) -> Dict[str, Any]:
        why = "understand anything beyond the fixed commands"
        info = self.host.brain.info()
        if self._pulling and self._pulling.poll() is None:
            return _row("brain", "AI model", why, "waiting",
                        "downloading the local model (about 5 GB)…", required=False)
        if info.get("status") == "ready":
            return _row("brain", "AI model", why, "ok", str(info.get("detail", "")),
                        required=False)
        if platforms.ollama():
            return _row("brain", "AI model", why, "optional",
                        "Ollama is installed but has no model — download one (about 5 GB)",
                        action="pull_model", required=False)
        return _row("brain", "AI model", why, "optional",
                    "fixed commands only. Install Ollama for a free local model, or "
                    "set ANTHROPIC_API_KEY to use Claude", action="get_ollama", required=False)

    def _models(self) -> Dict[str, Any]:
        roots = [str(r) for r in self.host.models.roots]
        count = len(self.host.models.scan())
        names = ", ".join(pathlib.Path(r).name for r in roots) or "no folders"
        detail = f"{count} models in {names}"
        if not platforms.openscad():
            detail += " · install OpenSCAD to open .scad files"
        return _row("models", "3D model folders", "the files you can open and turn by hand",
                    "ok" if count else "optional", detail, action="add_folder",
                    required=False)

    # ------------------------------------------------------------ requests

    def request(self, id: str, arg: str = "") -> str:
        """Perform a row's action; returns a sentence for the panel."""
        if id == "control" and PLATFORM == "macos":
            if self.saved.get("restart_needed") and not desktop.accessibility_trusted():
                return self.restart()
            desktop.request_accessibility()
            desktop.open_privacy_pane("Accessibility")
            self._mark("control", restart=True)
            return "Allow JARVIS in the list, then press Restart"
        if id == "automation":
            return self._ask_automation()
        if id == "screen":
            try:
                import Quartz
                Quartz.CGRequestScreenCaptureAccess()
            except Exception:
                pass
            desktop.open_privacy_pane("ScreenCapture")
            return "Allow JARVIS under Screen Recording"
        if id in ("camera", "microphone"):
            row = self._camera() if id == "camera" else self._microphone()
            if row["action"] == "get_browser":
                return self._open("https://www.google.com/chrome/")
            desktop.open_privacy_pane("Camera" if id == "camera" else "Microphone")
            return "Turn it on in the settings that just opened"
        if id == "brain":
            if platforms.ollama():
                return self._pull()
            return self._open("https://ollama.com/download")
        if id == "models" and arg:
            return self._add_folder(arg)
        if id == "restart":
            return self.restart()
        return "nothing to do"

    def _mark(self, id: str, restart: bool = False) -> None:
        self.saved.setdefault("asked", {})[id] = time.time()
        if restart:
            self.saved["restart_needed"] = True
        _save(self.saved)

    def seen(self) -> None:
        self.saved["setup_seen"] = True
        _save(self.saved)

    def _open(self, url: str) -> str:
        try:
            desktop.open_url(url)
        except Exception:
            pass
        return f"opened {url}"

    def _ask_automation(self) -> str:
        # Any Apple Event to System Events raises the prompt the first time;
        # the answer is remembered by macOS, and here, so it is asked once.
        proc = subprocess.run(["osascript", "-e",
                               'tell application "System Events" to count processes'],
                              capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            self.saved["automation"] = "ok"
            _save(self.saved)
            return "App control granted"
        if "-1743" in proc.stderr or "not allowed" in proc.stderr.lower():
            self.saved["automation"] = "denied"
            _save(self.saved)
            desktop.open_privacy_pane("Automation")
            return "Turn on JARVIS › System Events"
        return proc.stderr.strip()[:160] or "could not ask"

    def _pull(self) -> str:
        if self._pulling and self._pulling.poll() is None:
            return "already downloading"
        exe = platforms.ollama()
        model = self.host.cfg["ai"].get("local_model") or "qwen3:8b"
        log = open(config_module.ROOT / "logs" / "ollama-pull.log", "ab")
        self._pulling = subprocess.Popen([exe, "pull", model], stdout=log, stderr=log,
                                         stdin=subprocess.DEVNULL)
        return f"downloading {model} — this takes a while; JARVIS switches to it when done"

    def _add_folder(self, raw: str) -> str:
        path = pathlib.Path(raw.strip().strip('"')).expanduser()
        if not path.is_dir():
            return f"no folder at {path}"
        roots = [str(r) for r in self.host.models.roots]
        if str(path) not in roots:
            roots.append(str(path))
        config_module.save_patch({"models": {"roots": roots}})
        from .models import ModelIndex
        self.host.models.roots = ModelIndex(roots).roots
        self.host.models.scan(force=True)
        return f"added {path.name}"

    # ------------------------------------------------------------- restart

    def restart(self) -> str:
        """Relaunch, so a new grant applies: macOS evaluates Accessibility for
        a process when it starts, never again while it runs."""
        self.saved["restart_needed"] = False
        _save(self.saved)
        bundle = _bundle_path()
        if bundle:
            cmd = ["/bin/sh", "-c", f'sleep 3; open -n "{bundle}"']
        else:
            cmd = [sys.executable, "-m", "jarvis.main", "--log"]
            cmd = ["/bin/sh", "-c", "sleep 3; cd " + _sh(str(config_module.ROOT)) + " && "
                   + " ".join(_sh(c) for c in cmd) + " &"] if PLATFORM != "windows" else cmd
        kw = {"start_new_session": True} if PLATFORM != "windows" else {
            "creationflags": 0x00000008}   # DETACHED_PROCESS
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, cwd=str(config_module.ROOT), **kw)
        self.host.schedule_power_down()
        return "restarting"

    # ---------------------------------------------------------- first load

    def first_load(self) -> None:
        """Raise the operating system's own prompts once, on the first start."""
        if PLATFORM != "macos" or self.saved.get("first_load_done"):
            return
        if os.environ.get("JARVIS_NO_PROMPTS"):      # tests and CI: never a real dialog
            return
        self.saved["first_load_done"] = True
        _save(self.saved)
        if not (desktop.accessibility_trusted() or desktop.relay_available()):
            desktop.request_accessibility()
            self._mark("control", restart=True)
        if "automation" not in self.saved:
            self._ask_automation()


def _sh(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def _bundle_path() -> Optional[str]:
    """The JARVIS.app this was launched from, if any."""
    if os.environ.get("JARVIS_BUNDLED") != "1":
        return None
    try:
        args = subprocess.run(["ps", "-o", "args=", "-p", str(os.getppid())],
                              capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    marker = ".app/Contents/MacOS"
    if marker not in args:
        return None
    return args.split(marker)[0].strip() + ".app"
