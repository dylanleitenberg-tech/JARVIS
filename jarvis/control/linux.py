"""Linux control surface. Same functions as macos.py.

Linux has no single desktop, so this uses what a desktop session normally has
and says what is missing when it is not: pyautogui (XTest) for keyboard and
mouse, wmctrl/xdotool for windows, pactl or amixer for sound, playerctl for
media, loginctl for locking, notify-send, wl-clipboard or xclip.

Wayland is the hard limit. It does not let one program type or click into
another, by design, so on a Wayland session keyboard and mouse control reach
only X11 (XWayland) windows. The permissions check says so rather than letting
every click silently vanish.
"""
from __future__ import annotations

import os
import pathlib
import re
import shlex
import shutil
import subprocess
from typing import Dict, List, Optional

import psutil

from . import _portable
from ._portable import *  # noqa: F401,F403
from ._portable import (ControlError, _gui, _have, _procs_named, _run, _stem,
                        press_key)

# X11 scroll is in wheel notches, not pixels: 120 "pixels" is one notch-ish.
_portable.SCROLL_UNIT = 1 / 40.0


def wayland() -> bool:
    return (os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
            or bool(os.environ.get("WAYLAND_DISPLAY")))


def accessibility_trusted() -> bool:
    """Whether synthetic input can reach other applications."""
    if wayland() or not os.environ.get("DISPLAY"):
        return False
    try:
        _gui()
        return True
    except ControlError:
        return False


def request_accessibility() -> bool:
    return accessibility_trusted()


def open_privacy_pane(pane: str = "Camera") -> str:
    for tool in (["gnome-control-center", "privacy"], ["systemsettings"]):
        if _have(tool[0]):
            subprocess.Popen(tool, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"opened {pane} settings"
    return "open your desktop's privacy settings"


def _need(tool: str, what: str) -> None:
    if not _have(tool):
        raise ControlError(f"{what} needs {tool} (install it with your package manager)")


# ------------------------------------------------------------------ windows

def list_windows() -> List[Dict[str, object]]:
    if not _have("wmctrl"):
        return []
    out = _run(["wmctrl", "-lpG"]).stdout
    windows = []
    for line in out.splitlines():
        parts = line.split(None, 8)
        if len(parts) < 8:
            continue
        wid, _desk, pid, x, y, w, h = parts[:7]
        title = parts[8] if len(parts) > 8 else ""
        try:
            app = _stem(psutil.Process(int(pid)).name())
        except (psutil.Error, ValueError):
            app = ""
        windows.append({"id": int(wid, 16), "app": app, "title": title,
                        "x": int(x), "y": int(y), "w": int(w), "h": int(h)})
    return windows


def list_apps() -> List[str]:
    seen: List[str] = []
    for w in list_windows():
        if w["app"] and w["app"] not in seen:
            seen.append(str(w["app"]))
    return seen


def frontmost_app() -> str:
    if _have("xdotool"):
        pid = _run(["xdotool", "getactivewindow", "getwindowpid"]).stdout.strip()
        if pid.isdigit():
            try:
                return _stem(psutil.Process(int(pid)).name())
            except psutil.Error:
                pass
    return ""


def activate_app(name: str) -> str:
    if _have("wmctrl") and _run(["wmctrl", "-xa", name]).returncode == 0:
        return f"focused {name}"
    if _have("xdotool") and _run(["xdotool", "search", "--onlyvisible", "--class", name,
                                  "windowactivate"]).returncode == 0:
        return f"focused {name}"
    return open_app(name)


def hide_app(name: Optional[str] = None) -> str:
    return minimize_window()


def close_window() -> str:
    press_key("alt+f4")
    return "window closed"


def minimize_window() -> str:
    _need("xdotool", "minimising a window")
    _run(["xdotool", "getactivewindow", "windowminimize"])
    return "window minimised"


def fullscreen_window() -> str:
    if _have("wmctrl"):
        _run(["wmctrl", "-r", ":ACTIVE:", "-b", "toggle,fullscreen"])
    else:
        press_key("f11")
    return "fullscreen toggled"


def move_window(x: int, y: int, w: int, h: int, app: Optional[str] = None) -> str:
    _need("wmctrl", "moving a window")
    target = ["-r", app] if app else ["-r", ":ACTIVE:"]
    _run(["wmctrl", *target, "-b", "remove,maximized_vert,maximized_horz"])
    _run(["wmctrl", *target, "-e", f"0,{int(x)},{int(y)},{int(w)},{int(h)}"])
    return f"window -> {w}x{h} at {x},{y}"


def snap_window(where: str, app: Optional[str] = None) -> str:
    sw, sh = _portable.screen_size()
    top = 32                                   # most panels are about this tall
    usable = sh - top
    layouts = {
        "left": (0, top, sw // 2, usable), "right": (sw // 2, top, sw // 2, usable),
        "top": (0, top, sw, usable // 2), "bottom": (0, top + usable // 2, sw, usable // 2),
        "full": (0, top, sw, usable),
        "center": (sw // 6, top + usable // 8, (sw * 2) // 3, (usable * 3) // 4),
    }
    if where not in layouts:
        raise ControlError(f"unknown position {where!r}")
    return move_window(*layouts[where], app=app)


def mission_control() -> str:
    press_key("win")                           # GNOME Activities, KDE Overview
    return "overview"


def show_desktop() -> str:
    press_key("win+d")
    return "desktop"


def switch_space(index: int) -> str:
    _need("wmctrl", "switching workspaces")
    _run(["wmctrl", "-s", str(max(0, int(index) - 1))])
    return f"space {index}"


# ---------------------------------------------------------------------- apps

_APP_DIRS = ["/usr/share/applications", "/usr/local/share/applications",
             "~/.local/share/applications", "/var/lib/flatpak/exports/share/applications",
             "~/.local/share/flatpak/exports/share/applications",
             "/var/lib/snapd/desktop/applications"]


def _desktop_entries() -> Dict[str, pathlib.Path]:
    entries: Dict[str, pathlib.Path] = {}
    for folder in _APP_DIRS:
        root = pathlib.Path(folder).expanduser()
        if not root.is_dir():
            continue
        for path in root.glob("*.desktop"):
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            match = re.search(r"^Name=(.+)$", text, re.M)
            if match and "NoDisplay=true" not in text:
                entries.setdefault(match.group(1).strip().lower(), path)
    return entries


def resolve_app(name: str) -> str:
    key = name.strip().lower()
    aliases = {"browser": "web browser", "files": "files", "finder": "files",
               "terminal": "terminal", "code": "visual studio code",
               "vs code": "visual studio code", "vscode": "visual studio code",
               "chrome": "google chrome", "settings": "settings",
               "activity monitor": "system monitor"}
    key = aliases.get(key, key)
    entries = _desktop_entries()
    for test in (lambda s: s == key, lambda s: s.startswith(key), lambda s: key in s):
        for label, path in entries.items():
            if test(label):
                return str(path)
    return name.strip()


def open_app(name: str) -> str:
    target = resolve_app(name)
    if target.endswith(".desktop"):
        if _have("gtk-launch"):
            proc = _run(["gtk-launch", pathlib.Path(target).stem])
            if proc.returncode == 0:
                return f"opened {name}"
        text = pathlib.Path(target).read_text(errors="replace")
        match = re.search(r"^Exec=(.+)$", text, re.M)
        if match:
            argv = [a for a in shlex.split(match.group(1)) if not a.startswith("%")]
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return f"opened {name}"
    exe = shutil.which(target.lower().replace(" ", "-")) or \
        shutil.which(target.lower().replace(" ", ""))
    if exe:
        subprocess.Popen([exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return f"opened {name}"
    raise ControlError(f"no application named {name!r}")


def open_file(path: str, app: Optional[str] = None) -> str:
    target = pathlib.Path(path).expanduser()
    if not target.exists():
        raise ControlError(f"no such file: {target}")
    if app:
        from .. import platforms
        exe = platforms.app_executable(app)
        if exe:
            subprocess.Popen([exe, str(target)], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return f"opened {target.name} in {app}"
    subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return f"opened {target.name}"


# -------------------------------------------------------------------- system

def media_key(name: str) -> str:
    verbs = {"play_pause": "play-pause", "next_track": "next", "prev_track": "previous"}
    if name in verbs and _have("playerctl"):
        _run(["playerctl", verbs[name]])
        return f"media: {name}"
    if name in ("volume_up", "volume_down"):
        return volume_step(5 if name == "volume_up" else -5)
    if name == "mute" and _have("pactl"):
        _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"])
        return "media: mute"
    return _portable.media_key(name)


def get_volume() -> int:
    if _have("pactl"):
        out = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"]).stdout
        match = re.search(r"(\d+)%", out)
        if match:
            return int(match.group(1))
    if _have("amixer"):
        match = re.search(r"\[(\d+)%\]", _run(["amixer", "get", "Master"]).stdout)
        if match:
            return int(match.group(1))
    raise ControlError("reading the volume needs pactl or amixer")


def set_volume(level: int) -> str:
    level = max(0, min(int(level), 100))
    if _have("pactl"):
        _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])
    elif _have("amixer"):
        _run(["amixer", "-q", "set", "Master", f"{level}%"])
    else:
        raise ControlError("setting the volume needs pactl or amixer")
    return f"volume {level}"


def volume_step(delta: int = 10) -> str:
    return set_volume(get_volume() + int(delta))


def set_mute(muted: bool = True) -> str:
    if _have("pactl"):
        _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0"])
    elif _have("amixer"):
        _run(["amixer", "-q", "set", "Master", "mute" if muted else "unmute"])
    else:
        raise ControlError("muting needs pactl or amixer")
    return "muted" if muted else "unmuted"


def set_brightness(direction: str = "up", steps: int = 2) -> str:
    _need("brightnessctl", "changing brightness")
    amount = f"{10 * max(1, int(steps))}%"
    _run(["brightnessctl", "set", f"+{amount}" if direction == "up" else f"{amount}-"])
    return f"brightness {direction}"


def screenshot(path: Optional[str] = None, interactive: bool = False) -> str:
    if interactive and _have("gnome-screenshot"):
        subprocess.Popen(["gnome-screenshot", "-i"])
        return "screenshot tool open"
    return _portable.screenshot(path)


def lock_screen() -> str:
    if _run(["loginctl", "lock-session"]).returncode != 0 and _have("xdg-screensaver"):
        _run(["xdg-screensaver", "lock"])
    return "screen locked"


def sleep_display() -> str:
    _need("xset", "turning the display off")
    _run(["xset", "dpms", "force", "off"])
    return "display asleep"


def notify(text: str, title: str = "J.A.R.V.I.S.") -> str:
    _need("notify-send", "notifications")
    _run(["notify-send", title, text])
    return "notified"


def speak(text: str, voice: str = "", rate: int = 190) -> str:
    for tool in ("spd-say", "espeak-ng", "espeak"):
        if _have(tool):
            subprocess.Popen([tool, text])
            return "speaking"
    raise ControlError("no speech synthesiser (spd-say or espeak)")


def clipboard_get() -> str:
    for args in (["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"],
                 ["xsel", "-b", "-o"]):
        if _have(args[0]):
            return _run(args).stdout
    raise ControlError("reading the clipboard needs wl-clipboard or xclip")


def clipboard_set(text: str) -> str:
    for args in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-b", "-i"]):
        if _have(args[0]):
            subprocess.run(args, input=text, text=True, check=False)
            return "copied to clipboard"
    raise ControlError("writing the clipboard needs wl-clipboard or xclip")


def wifi_name() -> str:
    if not _have("nmcli"):
        return "unknown"
    for line in _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).stdout.splitlines():
        if line.startswith("yes:"):
            return line.split(":", 1)[1]
    return "not joined"


def system_status() -> Dict[str, object]:
    status = _portable.system_status()
    try:
        status["wifi"] = wifi_name()
    except Exception:
        pass
    return status
