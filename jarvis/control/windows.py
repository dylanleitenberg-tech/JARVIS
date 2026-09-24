"""Windows control surface. Same functions as macos.py.

Input is SendInput (through pyautogui); windows are user32 through ctypes;
apps launch from their Start-menu shortcuts, which is where Windows keeps the
names people actually say. Nothing here needs a permission: Windows does not
gate synthetic input, except into windows running as administrator, which no
unelevated program can type into.
"""
from __future__ import annotations

import ctypes
import os
import pathlib
import subprocess
import time
from typing import Dict, List, Optional, Tuple

import psutil

from ._portable import *  # noqa: F401,F403
from ._portable import (ControlError, _app_is_running, _gui, _procs_named,
                        _run, _stem, press_key)

_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
_NO_WINDOW = 0x08000000        # CREATE_NO_WINDOW: no console flash per call


def accessibility_trusted() -> bool:
    return True


def request_accessibility() -> bool:
    return True


_PANES = {"Camera": "ms-settings:privacy-webcam",
          "Microphone": "ms-settings:privacy-microphone",
          "Accessibility": "ms-settings:easeofaccess",
          "ScreenCapture": "ms-settings:privacy-graphicsCaptureProgrammatic",
          "Automation": "ms-settings:privacy"}


def open_privacy_pane(pane: str = "Camera") -> str:
    os.startfile(_PANES.get(pane, "ms-settings:privacy"))
    return f"opened {pane} settings"


def _ps(script: str, timeout: float = 15.0, wait: bool = True):
    args = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    if not wait:
        return subprocess.Popen(args, creationflags=_NO_WINDOW,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return _run(args, timeout=timeout, creationflags=_NO_WINDOW)


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# ------------------------------------------------------------------ windows

def _windows() -> List[Dict[str, object]]:
    """Visible top-level windows with a title, front to back."""
    if _user32 is None:
        return []
    found: List[Dict[str, object]] = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = (ctypes.c_long * 4)()
        _user32.GetWindowRect(hwnd, ctypes.byref(rect))
        try:
            app = _stem(psutil.Process(pid.value).name())
        except psutil.Error:
            app = ""
        found.append({"id": int(hwnd), "app": app, "title": buf.value, "pid": pid.value,
                      "x": rect[0], "y": rect[1], "w": rect[2] - rect[0], "h": rect[3] - rect[1]})
        return True

    _user32.EnumWindows(proto(visit), 0)
    return found


def list_windows() -> List[Dict[str, object]]:
    return [{k: v for k, v in w.items() if k != "pid"} for w in _windows()]


def list_apps() -> List[str]:
    seen: List[str] = []
    for w in _windows():
        if w["app"] and w["app"] not in seen and w["app"].lower() not in ("explorer", "textinputhost"):
            seen.append(str(w["app"]))
    return seen


def _front() -> int:
    return int(_user32.GetForegroundWindow()) if _user32 else 0


def frontmost_app() -> str:
    hwnd = _front()
    for w in _windows():
        if w["id"] == hwnd:
            return str(w["app"])
    return ""


def _window_of(name: Optional[str]) -> int:
    if not name:
        return _front()
    want = _stem(resolve_app(name)).lower()
    for w in _windows():
        if str(w["app"]).lower().startswith(want) or want in str(w["title"]).lower():
            return int(w["id"])
    return 0


_SW_MINIMIZE, _SW_MAXIMIZE, _SW_RESTORE = 6, 3, 9


def activate_app(name: str) -> str:
    hwnd = _window_of(name)
    if not hwnd:
        return open_app(name)
    _user32.ShowWindow(hwnd, _SW_RESTORE)
    # Windows refuses SetForegroundWindow from a background process unless a
    # key was just pressed; a bare alt tap is the documented way round it.
    _gui().press("alt")
    _user32.SetForegroundWindow(hwnd)
    return f"focused {name}"


def hide_app(name: Optional[str] = None) -> str:
    hwnd = _window_of(name)
    if not hwnd:
        raise ControlError(f"no window for {name or 'the front app'}")
    _user32.ShowWindow(hwnd, _SW_MINIMIZE)
    return f"hid {name or frontmost_app()}"


def close_window() -> str:
    press_key("alt+f4")
    return "window closed"


def minimize_window() -> str:
    _user32.ShowWindow(_front(), _SW_MINIMIZE)
    return "window minimised"


def fullscreen_window() -> str:
    hwnd = _front()
    _user32.ShowWindow(hwnd, _SW_RESTORE if _user32.IsZoomed(hwnd) else _SW_MAXIMIZE)
    return "fullscreen toggled"


def move_window(x: int, y: int, w: int, h: int, app: Optional[str] = None) -> str:
    hwnd = _window_of(app)
    if not hwnd:
        raise ControlError(f"no window for {app or 'the front app'}")
    _user32.ShowWindow(hwnd, _SW_RESTORE)
    _user32.MoveWindow(hwnd, int(x), int(y), int(w), int(h), True)
    return f"window -> {w}x{h} at {x},{y}"


def _work_area() -> Tuple[int, int, int, int]:
    rect = (ctypes.c_long * 4)()
    _user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)   # SPI_GETWORKAREA
    return rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]


def snap_window(where: str, app: Optional[str] = None) -> str:
    x0, y0, sw, sh = _work_area()
    layouts = {
        "left": (x0, y0, sw // 2, sh), "right": (x0 + sw // 2, y0, sw // 2, sh),
        "top": (x0, y0, sw, sh // 2), "bottom": (x0, y0 + sh // 2, sw, sh // 2),
        "full": (x0, y0, sw, sh),
        "center": (x0 + sw // 6, y0 + sh // 8, (sw * 2) // 3, (sh * 3) // 4),
    }
    if where not in layouts:
        raise ControlError(f"unknown position {where!r}")
    return move_window(*layouts[where], app=app)


def mission_control() -> str:
    press_key("win+tab")
    return "task view"


def show_desktop() -> str:
    press_key("win+d")
    return "desktop"


def switch_space(index: int) -> str:
    raise ControlError("Windows can only step between desktops: say next or previous desktop")


# ---------------------------------------------------------------------- apps

_ALIASES = {
    "chrome": "chrome", "google chrome": "chrome", "browser": "msedge",
    "web browser": "msedge", "edge": "msedge", "microsoft edge": "msedge",
    "notepad": "notepad", "calculator": "calc", "paint": "mspaint",
    "file explorer": "explorer", "explorer": "explorer", "files": "explorer",
    "finder": "explorer", "terminal": "wt", "command prompt": "cmd",
    "powershell": "powershell", "task manager": "taskmgr",
    "activity monitor": "taskmgr", "settings": "ms-settings:",
    "system settings": "ms-settings:", "word": "winword", "excel": "excel",
    "powerpoint": "powerpnt", "outlook": "outlook",
    "code": "code", "vscode": "code", "vs code": "code",
    "visual studio code": "code",
}


def _start_menu() -> List[pathlib.Path]:
    roots = [pathlib.Path(os.environ.get("ProgramData", r"C:\ProgramData")),
             pathlib.Path(os.environ.get("APPDATA", ""))]
    links: List[pathlib.Path] = []
    for root in roots:
        folder = root / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        if folder.is_dir():
            links.extend(folder.rglob("*.lnk"))
    return links


def resolve_app(name: str) -> str:
    """A spoken name -> a Start-menu shortcut path, or a command Windows knows."""
    key = name.strip().lower()
    if key in _ALIASES:
        return _ALIASES[key]
    links = _start_menu()
    for test in (lambda s: s == key, lambda s: s.startswith(key), lambda s: key in s):
        for link in links:
            stem = link.stem.lower()
            if "uninstall" not in stem and test(stem):
                return str(link)
    return name.strip()


def open_app(name: str) -> str:
    target = resolve_app(name)
    if target.lower().endswith(".lnk") or target.endswith(":"):
        os.startfile(target)
        return f"opened {pathlib.Path(target).stem if target.endswith('.lnk') else name}"
    # `start` resolves App Paths (chrome, winword, code) the way the Run box does.
    proc = _run(["cmd", "/c", "start", "", target], creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise ControlError(f"no application named {name!r}")
    return f"opened {name}"


def open_file(path: str, app: Optional[str] = None) -> str:
    target = pathlib.Path(path).expanduser()
    if not target.exists():
        raise ControlError(f"no such file: {target}")
    if app:
        from .. import platforms
        exe = platforms.app_executable(app)
        if exe:
            subprocess.Popen([exe, str(target)], creationflags=_NO_WINDOW)
            return f"opened {target.name} in {app}"
    os.startfile(str(target))
    return f"opened {target.name}"


# -------------------------------------------------------------------- system

def _endpoint():
    """The default speaker's volume control, if pycaw is installed."""
    try:
        from pycaw.pycaw import AudioUtilities
        return AudioUtilities.GetSpeakers().EndpointVolume
    except Exception:
        return None


def set_volume(level: int) -> str:
    level = max(0, min(int(level), 100))
    ep = _endpoint()
    if ep is not None:
        ep.SetMasterVolumeLevelScalar(level / 100.0, None)
        return f"volume {level}"
    gui = _gui()                       # each key press is two percent
    gui.press("volumedown", presses=50)
    gui.press("volumeup", presses=level // 2)
    return f"volume {level}"


def get_volume() -> int:
    ep = _endpoint()
    if ep is None:
        raise ControlError("reading the volume needs pycaw")
    return int(round(ep.GetMasterVolumeLevelScalar() * 100))


def volume_step(delta: int = 10) -> str:
    ep = _endpoint()
    if ep is not None:
        return set_volume(get_volume() + int(delta))
    _gui().press("volumeup" if delta > 0 else "volumedown", presses=max(1, abs(int(delta)) // 2))
    return f"volume {'up' if delta > 0 else 'down'} {abs(int(delta))}"


def set_mute(muted: bool = True) -> str:
    ep = _endpoint()
    if ep is not None:
        ep.SetMute(1 if muted else 0, None)
    else:
        _gui().press("volumemute")          # a toggle, the best there is without pycaw
    return "muted" if muted else "unmuted"


def set_brightness(direction: str = "up", steps: int = 2) -> str:
    delta = 10 * max(1, int(steps)) * (1 if direction == "up" else -1)
    script = ("$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness; "
              "$b = [Math]::Max(0, [Math]::Min(100, $m.CurrentBrightness + (%d))); "
              "Invoke-CimMethod -InputObject (Get-CimInstance -Namespace root/WMI "
              "-ClassName WmiMonitorBrightnessMethods) -MethodName WmiSetBrightness "
              "-Arguments @{Timeout=1; Brightness=$b} | Out-Null" % delta)
    if _ps(script).returncode != 0:
        raise ControlError("this display's brightness is not controllable from Windows")
    return f"brightness {direction}"


def screenshot(path: Optional[str] = None, interactive: bool = False) -> str:
    if interactive:
        os.startfile("ms-screenclip:")
        return "snipping"
    from ._portable import screenshot as grab
    return grab(path)


def lock_screen() -> str:
    _user32.LockWorkStation()
    return "screen locked"


def sleep_display() -> str:
    _user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)     # SC_MONITORPOWER, off
    return "display asleep"


def notify(text: str, title: str = "J.A.R.V.I.S.") -> str:
    _ps("Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
        f"$n.ShowBalloonTip(6000, {_ps_quote(title)}, {_ps_quote(text)}, "
        "[System.Windows.Forms.ToolTipIcon]::None); Start-Sleep 7; $n.Dispose()", wait=False)
    return "notified"


def speak(text: str, voice: str = "", rate: int = 190) -> str:
    _ps("Add-Type -AssemblyName System.Speech; "
        "(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak("
        f"{_ps_quote(text)})", wait=False)
    return "speaking"


def clipboard_get() -> str:
    return _ps("Get-Clipboard -Raw").stdout.rstrip("\r\n")


def clipboard_set(text: str) -> str:
    _ps(f"Set-Clipboard -Value {_ps_quote(text)}")
    return "copied to clipboard"


def wifi_name() -> str:
    out = _run(["netsh", "wlan", "show", "interfaces"], creationflags=_NO_WINDOW).stdout
    for line in out.splitlines():
        if line.strip().startswith("SSID") and "BSSID" not in line:
            return line.split(":", 1)[1].strip()
    return "not joined"


def system_status() -> Dict[str, object]:
    from ._portable import system_status as base
    status = base()
    try:
        status["wifi"] = wifi_name()
    except Exception:
        pass
    return status
