"""What Windows and Linux control have in common.

Keyboard, mouse and screen go through pyautogui, which speaks SendInput on
Windows and XTest on Linux; processes go through psutil; the rest is the
standard library. windows.py and linux.py start from this and replace what
their platform does differently. macOS has its own module (macos.py) because
it needs the signed-app relay that neither of the others has.

Every function has the same name and the same return convention as macos.py:
a short sentence saying what happened, or ControlError saying why not.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import time
import webbrowser
from typing import Dict, List, Optional, Tuple

import psutil


class ControlError(RuntimeError):
    pass


_GUI = None


def _gui():
    """pyautogui, imported on first use: on Linux importing it opens the X
    display, which must not happen at import time on a machine without one."""
    global _GUI
    if _GUI is None:
        try:
            import pyautogui
        except Exception as exc:  # no display, or not installed
            raise ControlError(f"keyboard and mouse control unavailable: {exc}") from exc
        # The failsafe throws when the cursor reaches a corner, which a hand
        # steering the pointer does all the time; gesture control has its own
        # off switch. And the default 0.1 s pause after every call would turn a
        # smooth drag into a slideshow.
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0
        _GUI = pyautogui
    return _GUI


# --------------------------------------------------------------------- keys

# Combos arrive in macOS spelling ("cmd+t") from the model and the registry.
# Off the Mac, command is control: that is the shortcut people mean.
MODIFIERS = {
    "cmd": "ctrl", "command": "ctrl", "⌘": "ctrl",
    "ctrl": "ctrl", "control": "ctrl", "^": "ctrl",
    "alt": "alt", "opt": "alt", "option": "alt", "⌥": "alt",
    "shift": "shift", "⇧": "shift",
    "win": "win", "super": "win", "meta": "win", "windows": "win",
}

KEYS = {
    "return": "enter", "enter": "enter", "escape": "esc", "esc": "esc",
    "delete": "backspace", "backspace": "backspace", "forwarddelete": "delete",
    "space": "space", "tab": "tab", "home": "home", "end": "end",
    "pageup": "pageup", "pagedown": "pagedown",
    "left": "left", "right": "right", "up": "up", "down": "down",
}


def parse_combo(combo: str) -> Tuple[List[str], str]:
    """'cmd+shift+t' -> (['ctrl', 'shift'], 't')."""
    parts = [p.strip().lower() for p in combo.replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise ControlError(f"empty key combo: {combo!r}")
    mods = [MODIFIERS[p] for p in parts[:-1] if p in MODIFIERS]
    key = parts[-1]
    key = KEYS.get(key, key)
    return mods, key


def _key_name(key: str) -> str:
    return "winleft" if key == "win" else key


def press_key(combo: str) -> str:
    """Press a key combination, e.g. 'ctrl+t', 'alt+f4', 'escape'."""
    mods, key = parse_combo(combo)
    gui = _gui()
    if len(key) > 1 and _key_name(key) not in gui.KEYBOARD_KEYS:
        raise ControlError(f"unknown key: {key!r}")
    gui.hotkey(*[_key_name(m) for m in mods], _key_name(key))
    return f"pressed {combo}"


def type_text(text: str) -> str:
    """Type a string into the front window."""
    _gui().write(text, interval=0.005)
    return f"typed {len(text)} characters"


# -------------------------------------------------------------------- mouse

def screen_size() -> Tuple[int, int]:
    w, h = _gui().size()
    return int(w), int(h)


def mouse_position() -> Tuple[float, float]:
    x, y = _gui().position()
    return float(x), float(y)


def _clamp(x: float, y: float) -> Tuple[float, float]:
    w, h = screen_size()
    return max(0.0, min(float(x), w - 1.0)), max(0.0, min(float(y), h - 1.0))


def move_mouse(x: float, y: float) -> str:
    x, y = _clamp(x, y)
    _gui().moveTo(x, y)
    return f"cursor at {int(x)},{int(y)}"


def move_mouse_norm(nx: float, ny: float) -> str:
    w, h = screen_size()
    return move_mouse(nx * w, ny * h)


def click(button: str = "left", clicks: int = 1) -> str:
    _gui().click(button=button, clicks=max(1, int(clicks)), interval=0.02)
    return f"{button} click x{clicks}"


def _hold(modifiers, down: bool) -> None:
    gui = _gui()
    for name in (modifiers or ()):
        key = _key_name(MODIFIERS.get(str(name).lower(), str(name).lower()))
        (gui.keyDown if down else gui.keyUp)(key)


def mouse_down(button: str = "left", modifiers=None) -> str:
    """Press and hold a mouse button, with any modifier keys held first."""
    if button not in ("left", "right", "middle"):
        raise ControlError(f"unknown mouse button {button!r}")
    _hold(modifiers, True)
    _gui().mouseDown(button=button)
    return f"{button} down"


def mouse_up(button: str = "left", modifiers=None) -> str:
    _gui().mouseUp(button=button)
    _hold(list(reversed(list(modifiers or ()))), False)
    return f"{button} up"


def drag_to(x: float, y: float, button: str = "left", modifiers=None) -> str:
    """Continue a drag begun with mouse_down: moving with the button held is
    a drag to the window under the cursor on both platforms."""
    x, y = _clamp(x, y)
    _gui().moveTo(x, y)
    return f"drag to {int(x)},{int(y)}"


# Scroll arrives in macOS pixel units (a trackpad flick is about 120).
SCROLL_UNIT = 1.0


def scroll(dy: int = 0, dx: int = 0) -> str:
    gui = _gui()
    if dy:
        gui.scroll(int(round(dy * SCROLL_UNIT)) or (1 if dy > 0 else -1))
    if dx and hasattr(gui, "hscroll"):
        try:
            gui.hscroll(int(round(dx * SCROLL_UNIT)) or (1 if dx > 0 else -1))
        except Exception:
            pass
    return f"scroll {dx},{dy}"


_MEDIA_KEYS = {"play_pause": "playpause", "next_track": "nexttrack",
               "prev_track": "prevtrack", "volume_up": "volumeup",
               "volume_down": "volumedown", "mute": "volumemute"}


def media_key(name: str) -> str:
    if name not in _MEDIA_KEYS:
        raise ControlError(f"unknown media key {name!r}")
    _gui().press(_MEDIA_KEYS[name])
    return f"media: {name}"


# -------------------------------------------------------------------- apps

def _stem(name: str) -> str:
    return pathlib.Path(name).stem


def _procs_named(name: str) -> List[psutil.Process]:
    want = _stem(name).lower().replace(" ", "")
    found = []
    for proc in psutil.process_iter(["name"]):
        pname = _stem(proc.info.get("name") or "").lower().replace(" ", "")
        if pname and (pname == want or pname.startswith(want)):
            found.append(proc)
    return found


def _app_is_running(name: str) -> bool:
    return bool(_procs_named(name))


def quit_app(name: str) -> str:
    procs = _procs_named(name)
    if not procs:
        raise ControlError(f"{name} is not running")
    for proc in procs:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    return f"quit {name}"


# ------------------------------------------------ app menus (macOS only)
#
# OpenSCAD is driven through its menus only on the Mac, where UI scripting
# reaches them. Elsewhere it opens the file and previews it on its own.

def app_responsive(name: str) -> bool:
    return True


def wait_responsive(name: str, timeout: float = 90.0) -> bool:
    return True


def menu_checked(app: str, menu: str, item: str) -> Optional[bool]:
    return None


def click_menu(app: str, menu: str, item: str) -> str:
    raise ControlError("clicking another application's menus is macOS-only")


def set_menu_checked(app: str, menu: str, item: str, want: bool) -> str:
    return f"{item}: menus are macOS-only"


# ------------------------------------------------------------------ browser

def open_url(url: str, new_tab: bool = True) -> str:
    if not url.startswith(("http://", "https://", "file://")):
        url = "https://" + url
    webbrowser.open(url, new=2 if new_tab else 0)
    return f"opened {url}"


def search_web(query: str, engine: str = "google") -> str:
    from urllib.parse import quote_plus
    engines = {
        "google": "https://www.google.com/search?q=",
        "duckduckgo": "https://duckduckgo.com/?q=",
        "youtube": "https://www.youtube.com/results?search_query=",
        "wikipedia": "https://en.wikipedia.org/w/index.php?search=",
        "maps": "https://www.google.com/maps/search/",
    }
    return open_url(engines.get(engine, engines["google"]) + quote_plus(query))


def browser_tabs() -> List[Dict[str, str]]:
    return []          # no scripting bridge into the browser off the Mac


def browser_close_tab() -> str:
    press_key("ctrl+w")
    return "tab closed"


def browser_new_tab() -> str:
    press_key("ctrl+t")
    return "new tab"


def browser_select_tab(index: int) -> str:
    press_key(f"ctrl+{max(1, min(int(index), 9))}")
    return f"tab {index}"


def app_switcher() -> str:
    press_key("alt+tab")
    return "app switcher"


# ------------------------------------------------------------------ system

def desktop_dir() -> pathlib.Path:
    for cand in (pathlib.Path.home() / "Desktop",
                 pathlib.Path.home() / "OneDrive" / "Desktop"):
        if cand.is_dir():
            return cand
    return pathlib.Path.home()


def screenshot(path: Optional[str] = None, interactive: bool = False) -> str:
    if path is None:
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        path = str(desktop_dir() / f"JARVIS_{stamp}.png")
    try:
        from PIL import ImageGrab
        ImageGrab.grab(all_screens=True).save(path)
    except Exception as exc:
        raise ControlError(f"could not capture the screen: {exc}") from exc
    return f"captured {path}"


def run_shortcut(name: str, text_input: Optional[str] = None) -> str:
    raise ControlError("Shortcuts are a macOS feature")


def run_shell(command: str, timeout: float = 20.0) -> str:
    """Run a shell command. Disabled unless safety.allow_shell is true."""
    proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or proc.stderr).strip()
    return out[:4000] or f"exit {proc.returncode}"


def wifi_name() -> str:
    return "unknown"


def system_status() -> Dict[str, object]:
    vm = psutil.virtual_memory()
    status: Dict[str, object] = {
        "cpu": psutil.cpu_percent(interval=None),
        "cpu_cores": psutil.cpu_count(logical=True),
        "mem_percent": vm.percent,
        "mem_used_gb": round(vm.used / 1e9, 1),
        "mem_total_gb": round(vm.total / 1e9, 1),
        "uptime_h": round((time.time() - psutil.boot_time()) / 3600, 1),
        "processes": len(psutil.pids()),
    }
    try:
        root = (os.environ.get("SystemDrive", "C:") + "\\") if sys.platform == "win32" else "/"
        disk = psutil.disk_usage(root)
        status["disk_percent"] = disk.percent
        status["disk_free_gb"] = round(disk.free / 1e9, 1)
    except Exception:
        pass
    try:
        battery = psutil.sensors_battery()
        if battery:
            status["battery"] = round(battery.percent)
            status["charging"] = bool(battery.power_plugged)
            if battery.secsleft and battery.secsleft > 0:
                status["battery_hours"] = round(battery.secsleft / 3600, 1)
    except Exception:
        pass
    try:
        net = psutil.net_io_counters()
        status["net_sent_mb"] = round(net.bytes_sent / 1e6, 1)
        status["net_recv_mb"] = round(net.bytes_recv / 1e6, 1)
    except Exception:
        pass
    try:
        status["wifi"] = wifi_name()
    except Exception:
        status["wifi"] = "unknown"
    return status


# ------------------------------------------------------------- permissions

def relay_available() -> bool:
    return False       # the signed-app relay exists only on macOS


def _run(args: List[str], timeout: float = 10.0, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kw)


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None
