"""macOS control surface.

Everything J.A.R.V.I.S. can physically do to the machine lives here: apps,
windows, browser tabs, keyboard, mouse, volume, media, spaces, screenshots,
telemetry. Nothing in this module knows about the HUD or the language model —
it is a plain, synchronous library so it stays easy to test from a REPL.

Permissions this needs (System Settings -> Privacy & Security):
  * Accessibility  -> the terminal/app running J.A.R.V.I.S. (keystrokes, mouse,
    window control). Without it, keyboard/mouse actions silently do nothing.
  * Screen Recording -> only for `screenshot` of other apps' windows.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

import psutil

try:  # pyobjc; present on macOS, optional so the module still imports elsewhere
    import Quartz
    from AppKit import NSEvent, NSPasteboard, NSStringPboardType, NSSystemDefined
    HAVE_QUARTZ = True
except Exception:  # pragma: no cover
    HAVE_QUARTZ = False


class ControlError(RuntimeError):
    pass


# ---------------------------------------------------------------- AppleScript

def osascript(script: str, timeout: float = 12.0) -> str:
    """Run an AppleScript snippet and return stdout (stripped)."""
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=timeout,
    )
    if proc.returncode != 0:
        raise ControlError((proc.stderr or proc.stdout).strip() or "osascript failed")
    return proc.stdout.strip()


def _as_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


# ------------------------------------------------------------ control relay
#
# Accessibility is checked against the code signature of the process that calls
# AXIsProcessTrusted — unlike the camera and microphone, it is NOT attributed to
# the responsible app. A Python child of JARVIS.app therefore can never be
# trusted, however the app is ticked. So when the app IS trusted, every
# synthetic event is handed to the app binary over a unix socket and posted
# from there; otherwise these calls fall back to posting directly, which works
# whenever this process happens to be trusted in its own right.

_RELAY_SOCK = os.environ.get("JARVIS_CONTROL_SOCK", "")
_APP_TRUSTED = os.environ.get("JARVIS_APP_TRUSTED") == "1"
_relay_lock = threading.Lock()
_relay: Optional["socket.socket"] = None


def relay_available() -> bool:
    return bool(_APP_TRUSTED and _RELAY_SOCK and os.path.exists(_RELAY_SOCK))


def _relay_send(line: str) -> bool:
    """Send one command to the app binary. False if the relay is unusable."""
    global _relay
    if not relay_available():
        return False
    with _relay_lock:
        for attempt in (0, 1):
            try:
                if _relay is None:
                    _relay = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    _relay.settimeout(2.0)
                    _relay.connect(_RELAY_SOCK)
                _relay.sendall((line + "\n").encode())
                _relay.recv(1)
                return True
            except OSError:
                try:
                    if _relay:
                        _relay.close()
                except OSError:
                    pass
                _relay = None
                if attempt:          # already retried with a fresh connection
                    return False
    return False


_BUTTON_ID = {"left": 0, "right": 1, "middle": 2}


def _flag_bits(modifiers) -> int:
    bits = 0
    for name in (modifiers or ()):
        canon = MODIFIERS.get(str(name).lower(), str(name).lower())
        bits |= {"command": 1, "shift": 2, "option": 4, "control": 8}.get(canon, 0)
    return bits


def accessibility_trusted() -> bool:
    """True when the host process may post synthetic events."""
    if not HAVE_QUARTZ:
        return False
    try:
        return bool(Quartz.AXIsProcessTrusted())
    except Exception:
        return False


def request_accessibility() -> bool:
    """Ask macOS to show the Accessibility prompt for this process.

    The dialog names whichever app macOS holds responsible for this process —
    usually the terminal it was launched from — and adds it to the list. That
    is why it matters *where* J.A.R.V.I.S. is launched from: a process started
    detached has no responsible app and the grant has nowhere to land.
    """
    if not HAVE_QUARTZ:
        return False
    try:
        options = {Quartz.kAXTrustedCheckOptionPrompt: True}
        return bool(Quartz.AXIsProcessTrustedWithOptions(options))
    except Exception:
        return False


def open_privacy_pane(pane: str = "Accessibility") -> str:
    """Open a specific Privacy & Security pane in System Settings."""
    valid = {"Accessibility", "Camera", "Microphone", "ScreenCapture",
             "ListenEvent", "Automation"}
    if pane not in valid:
        raise ControlError(f"unknown pane {pane!r}")
    subprocess.run(
        ["open", f"x-apple.systempreferences:com.apple.preference.security?Privacy_{pane}"],
        check=False)
    return f"opened {pane} settings"


# ------------------------------------------------------------------ keyboard

# Virtual key codes for the US layout. Enough to drive menus and shortcuts.
KEYCODES: Dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33,
    "i": 34, "p": 35, "return": 36, "enter": 36, "l": 37, "j": 38, "'": 39,
    "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    "tab": 48, "space": 49, "`": 50, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53,
    "f17": 64, "f18": 79, "f19": 80, "f20": 90,
    "f5": 96, "f6": 97, "f7": 98, "f3": 99, "f8": 100, "f9": 101, "f11": 103,
    "f13": 105, "f16": 106, "f14": 107, "f10": 109, "f12": 111, "f15": 113,
    "help": 114, "home": 115, "pageup": 116, "forwarddelete": 117,
    "f4": 118, "end": 119, "f2": 120, "pagedown": 121, "f1": 122,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "brightnessdown": 145, "brightnessup": 144,
}

MODIFIERS = {
    "cmd": "command", "command": "command", "⌘": "command",
    "ctrl": "control", "control": "control", "^": "control",
    "alt": "option", "opt": "option", "option": "option", "⌥": "option",
    "shift": "shift", "⇧": "shift",
    "fn": "function",
}

_MOD_FLAGS = {
    "command": 1 << 20, "shift": 1 << 17, "option": 1 << 19,
    "control": 1 << 18, "function": 1 << 23,
}


def parse_combo(combo: str) -> Tuple[List[str], str]:
    """'cmd+shift+t' -> (['command','shift'], 't')."""
    parts = [p.strip().lower() for p in combo.replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise ControlError(f"empty key combo: {combo!r}")
    mods = [MODIFIERS[p] for p in parts[:-1] if p in MODIFIERS]
    key = parts[-1]
    return mods, key


def press_key(combo: str) -> str:
    """Press a key combination, e.g. 'cmd+t', 'cmd+shift+4', 'escape'."""
    mods, key = parse_combo(combo)
    if key in KEYCODES and _relay_send(f"key {KEYCODES[key]} {_flag_bits(mods)}"):
        return f"pressed {combo}"
    using = " using {" + ", ".join(f"{m} down" for m in mods) + "}" if mods else ""
    if key in KEYCODES:
        script = f'tell application "System Events" to key code {KEYCODES[key]}{using}'
    elif len(key) == 1:
        script = f'tell application "System Events" to keystroke "{_as_escape(key)}"{using}'
    else:
        raise ControlError(f"unknown key: {key!r}")
    osascript(script)
    return f"pressed {combo}"


def type_text(text: str) -> str:
    """Type a string into the frontmost app."""
    osascript(f'tell application "System Events" to keystroke "{_as_escape(text)}"')
    return f"typed {len(text)} characters"


# --------------------------------------------------------------------- mouse

def screen_size() -> Tuple[int, int]:
    if HAVE_QUARTZ:
        frame = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return int(frame.size.width), int(frame.size.height)
    out = osascript('tell application "Finder" to get bounds of window of desktop')
    _, _, w, h = [int(v.strip()) for v in out.split(",")]
    return w, h


def mouse_position() -> Tuple[float, float]:
    if not HAVE_QUARTZ:
        raise ControlError("Quartz unavailable")
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return float(loc.x), float(loc.y)


def _post_mouse(event_type, x: float, y: float, button=0, clicks: int = 1) -> None:
    event = Quartz.CGEventCreateMouseEvent(None, event_type, (x, y), button)
    if clicks > 1:
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, clicks)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def move_mouse(x: float, y: float) -> str:
    """Move the cursor to absolute screen pixels."""
    w, h = screen_size()
    x, y = max(0.0, min(float(x), w - 1.0)), max(0.0, min(float(y), h - 1.0))
    if _relay_send(f"move {x:.1f} {y:.1f}"):
        return f"cursor at {int(x)},{int(y)}"
    if not HAVE_QUARTZ:
        raise ControlError("Quartz unavailable")
    _post_mouse(Quartz.kCGEventMouseMoved, x, y)
    return f"cursor at {int(x)},{int(y)}"


def move_mouse_norm(nx: float, ny: float) -> str:
    """Move the cursor using 0..1 normalised coordinates (what vision produces)."""
    w, h = screen_size()
    return move_mouse(nx * w, ny * h)


def click(button: str = "left", clicks: int = 1) -> str:
    if _relay_send(f"click 0 0 {_BUTTON_ID.get(button, 0)} 0 {max(1, int(clicks))}"):
        return f"{button} click x{clicks}"
    if not HAVE_QUARTZ:
        raise ControlError("Quartz unavailable")
    x, y = mouse_position()
    if button == "right":
        down, up, btn = Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight
    else:
        down, up, btn = Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft
    for n in range(1, clicks + 1):
        _post_mouse(down, x, y, btn, n)
        _post_mouse(up, x, y, btn, n)
        time.sleep(0.02)
    return f"{button} click x{clicks}"


# down, up, dragged, button — middle is what most CAD viewports orbit with.
def _button_events(button: str):
    if not HAVE_QUARTZ:
        raise ControlError("Quartz unavailable")
    table = {
        "left":   (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp,
                   Quartz.kCGEventLeftMouseDragged, Quartz.kCGMouseButtonLeft),
        "right":  (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp,
                   Quartz.kCGEventRightMouseDragged, Quartz.kCGMouseButtonRight),
        "middle": (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp,
                   Quartz.kCGEventOtherMouseDragged, Quartz.kCGMouseButtonCenter),
    }
    if button not in table:
        raise ControlError(f"unknown mouse button {button!r}")
    return table[button]


def _flags_for(modifiers) -> int:
    """CGEvent flag mask for names like ['shift', 'control']."""
    mask = 0
    for name in (modifiers or ()):
        mask |= _MOD_FLAGS.get(MODIFIERS.get(str(name).lower(), str(name).lower()), 0)
    return mask


def _post_button(event_type, x: float, y: float, button, flags: int = 0) -> None:
    event = Quartz.CGEventCreateMouseEvent(None, event_type, (x, y), button)
    if flags:
        Quartz.CGEventSetFlags(event, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def mouse_down(button: str = "left", modifiers=None) -> str:
    """Press and hold a mouse button, optionally with modifier keys held."""
    if _relay_send(f"down 0 0 {_BUTTON_ID.get(button, 0)} {_flag_bits(modifiers)}"):
        return f"{button} down"
    down, _up, _drag, btn = _button_events(button)
    x, y = mouse_position()
    _post_button(down, x, y, btn, _flags_for(modifiers))
    return f"{button} down"


def mouse_up(button: str = "left", modifiers=None) -> str:
    if _relay_send(f"up 0 0 {_BUTTON_ID.get(button, 0)} {_flag_bits(modifiers)}"):
        return f"{button} up"
    _down, up, _drag, btn = _button_events(button)
    x, y = mouse_position()
    _post_button(up, x, y, btn, _flags_for(modifiers))
    return f"{button} up"


def drag_to(x: float, y: float, button: str = "left", modifiers=None) -> str:
    """Continue an in-progress drag (mouse_down must have run first)."""
    w, h = screen_size()
    x2, y2 = max(0.0, min(float(x), w - 1.0)), max(0.0, min(float(y), h - 1.0))
    if _relay_send(f"drag {x2:.1f} {y2:.1f} {_BUTTON_ID.get(button, 0)} "
                   f"{_flag_bits(modifiers)}"):
        return f"drag to {int(x2)},{int(y2)}"
    _down, _up, drag, btn = _button_events(button)
    x, y = max(0.0, min(float(x), w - 1.0)), max(0.0, min(float(y), h - 1.0))
    _post_button(drag, x, y, btn, _flags_for(modifiers))
    return f"drag to {int(x)},{int(y)}"


def scroll(dy: int = 0, dx: int = 0) -> str:
    if _relay_send(f"scroll {int(dy)} {int(dx)}"):
        return f"scroll {dx},{dy}"
    if not HAVE_QUARTZ:
        raise ControlError("Quartz unavailable")
    event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitPixel, 2,
                                                 int(dy), int(dx))
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    return f"scroll {dx},{dy}"


# ----------------------------------------------------------------- media keys

_MEDIA = {"play_pause": 16, "next_track": 17, "prev_track": 18,
          "volume_up": 0, "volume_down": 1, "mute": 7}


def media_key(name: str) -> str:
    """Press a media key (play/pause, next, previous)."""
    if name not in _MEDIA:
        raise ControlError(f"unknown media key {name!r}")
    if not HAVE_QUARTZ:
        raise ControlError("Quartz unavailable")
    code = _MEDIA[name]
    for down in (True, False):
        data = (code << 16) | ((0xA if down else 0xB) << 8)
        event = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            NSSystemDefined, (0, 0), 0xA00 if down else 0xB00, 0, 0, None, 8, data, -1)
        Quartz.CGEventPost(0, event.CGEvent())
    return f"media: {name}"


# ----------------------------------------------------------------------- apps

def list_apps() -> List[str]:
    """Visible (non-background) running applications."""
    out = osascript(
        'tell application "System Events" to get name of every application process '
        'whose background only is false')
    return [n.strip() for n in out.split(",") if n.strip()]


def frontmost_app() -> str:
    return osascript(
        'tell application "System Events" to get name of first application process '
        'whose frontmost is true')


def resolve_app(name: str) -> str:
    """Map a spoken name onto an installed application bundle name."""
    name = name.strip()
    aliases = {
        "chrome": "Google Chrome", "google chrome": "Google Chrome",
        "browser": "Google Chrome", "web browser": "Google Chrome",
        "code": "Visual Studio Code", "vscode": "Visual Studio Code",
        "vs code": "Visual Studio Code",
        "terminal": "Terminal", "iterm": "iTerm", "finder": "Finder",
        "notes": "Notes", "mail": "Mail", "messages": "Messages",
        "music": "Music", "spotify": "Spotify", "slack": "Slack",
        "calendar": "Calendar", "photos": "Photos", "preview": "Preview",
        "system settings": "System Settings", "settings": "System Settings",
        "safari": "Safari", "discord": "Discord", "zoom": "zoom.us",
        "activity monitor": "Activity Monitor", "calculator": "Calculator",
    }
    key = name.lower()
    if key in aliases:
        return aliases[key]
    # Fall back to a case-insensitive match against installed bundles.
    for folder in ("/Applications", "/System/Applications",
                   "/System/Applications/Utilities", "~/Applications"):
        try:
            import pathlib
            for entry in pathlib.Path(folder).expanduser().glob("*.app"):
                if entry.stem.lower() == key:
                    return entry.stem
        except Exception:
            continue
    return name


def open_app(name: str) -> str:
    """Launch or focus an application by name."""
    app = resolve_app(name)
    proc = subprocess.run(["open", "-a", app], capture_output=True, text=True)
    if proc.returncode != 0:
        raise ControlError(f"no application named {app!r}")
    return f"opened {app}"


def quit_app(name: str) -> str:
    """Quit an application by name."""
    app = resolve_app(name)
    osascript(f'tell application "{_as_escape(app)}" to quit')
    return f"quit {app}"


def hide_app(name: Optional[str] = None) -> str:
    app = resolve_app(name) if name else frontmost_app()
    osascript(f'tell application "System Events" to set visible of process "{_as_escape(app)}" to false')
    return f"hid {app}"


def activate_app(name: str) -> str:
    app = resolve_app(name)
    osascript(f'tell application "{_as_escape(app)}" to activate')
    return f"focused {app}"


# -------------------------------------------------------------------- windows

def list_windows() -> List[Dict[str, object]]:
    """Every on-screen window: owner, title, bounds."""
    if not HAVE_QUARTZ:
        return []
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    windows = []
    for info in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        if info.get("kCGWindowLayer", 0) != 0:
            continue
        bounds = info.get("kCGWindowBounds", {})
        windows.append({
            "id": int(info.get("kCGWindowNumber", 0)),
            "app": str(info.get("kCGWindowOwnerName", "")),
            "title": str(info.get("kCGWindowName", "") or ""),
            "x": int(bounds.get("X", 0)), "y": int(bounds.get("Y", 0)),
            "w": int(bounds.get("Width", 0)), "h": int(bounds.get("Height", 0)),
        })
    return windows


def close_window() -> str:
    """Close the frontmost window (cmd-W)."""
    press_key("cmd+w")
    return "window closed"


def minimize_window() -> str:
    press_key("cmd+m")
    return "window minimised"


def fullscreen_window() -> str:
    press_key("ctrl+cmd+f")
    return "fullscreen toggled"


def move_window(x: int, y: int, w: int, h: int, app: Optional[str] = None) -> str:
    """Position and size the front window of an app (needs Accessibility)."""
    target = _as_escape(resolve_app(app) if app else frontmost_app())
    osascript(
        f'tell application "System Events" to tell process "{target}" '
        f'to set position of front window to {{{int(x)}, {int(y)}}}')
    osascript(
        f'tell application "System Events" to tell process "{target}" '
        f'to set size of front window to {{{int(w)}, {int(h)}}}')
    return f"{target} window -> {w}x{h} at {x},{y}"


def snap_window(where: str, app: Optional[str] = None) -> str:
    """Snap the front window: left | right | top | bottom | full | center."""
    sw, sh = screen_size()
    menubar = 38
    usable_h = sh - menubar
    layouts = {
        "left":   (0, menubar, sw // 2, usable_h),
        "right":  (sw // 2, menubar, sw // 2, usable_h),
        "top":    (0, menubar, sw, usable_h // 2),
        "bottom": (0, menubar + usable_h // 2, sw, usable_h // 2),
        "full":   (0, menubar, sw, usable_h),
        "center": (sw // 6, menubar + usable_h // 8, (sw * 2) // 3, (usable_h * 3) // 4),
    }
    if where not in layouts:
        raise ControlError(f"unknown position {where!r}")
    return move_window(*layouts[where], app=app)


def mission_control() -> str:
    press_key("ctrl+up")
    return "mission control"


def show_desktop() -> str:
    press_key("fn+f11")
    return "desktop"


def app_switcher() -> str:
    press_key("cmd+tab")
    return "app switcher"


def switch_space(index: int) -> str:
    """Jump to desktop/space 1-9 (requires the ctrl-number shortcuts to be on)."""
    press_key(f"ctrl+{int(index)}")
    return f"space {index}"


# -------------------------------------------------------------------- browser

def _browser_name() -> str:
    for app in ("Google Chrome", "Safari", "Brave Browser", "Arc", "Firefox"):
        try:
            if app in list_apps():
                return app
        except ControlError:
            break
    return "Google Chrome"


def open_url(url: str, new_tab: bool = True) -> str:
    """Open a URL in the default browser."""
    if not url.startswith(("http://", "https://", "file://")):
        url = "https://" + url
    subprocess.run(["open", url], check=False)
    return f"opened {url}"


def open_file(path: str, app: Optional[str] = None) -> str:
    """Open a file, optionally forcing which application opens it."""
    import pathlib as _pathlib
    target = _pathlib.Path(path).expanduser()
    if not target.exists():
        raise ControlError(f"no such file: {target}")
    if not app:
        proc = subprocess.run(["open", str(target)], capture_output=True, text=True)
        if proc.returncode != 0:
            raise ControlError(proc.stderr.strip() or f"could not open {target.name}")
        return f"opened {target.name}"

    resolved = resolve_app(app)
    # An application that is not running yet swallows the file. OpenSCAD shows
    # its "Welcome to OpenSCAD" launcher and the document never opens —
    # verified: one `open -a OpenSCAD file.scad` from cold leaves exactly one
    # window, the welcome screen. The same command a second time, with the app
    # up, opens the file. So launch first, wait for the process, then ask.
    cold = not _app_is_running(resolved)
    if cold:
        subprocess.run(["open", "-a", resolved], capture_output=True, text=True)
        deadline = time.time() + 20.0
        while time.time() < deadline and not _app_is_running(resolved):
            time.sleep(0.3)
        time.sleep(1.5)          # let it finish putting its first window up

    proc = subprocess.run(["open", "-a", resolved, str(target)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise ControlError(proc.stderr.strip() or f"could not open {target.name}")
    return f"opened {target.name} in {app}"


def _osa(script: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(["osascript", "-e", script], capture_output=True,
                          text=True, timeout=timeout)


def app_responsive(name: str) -> bool:
    """Whether an application is answering UI scripting right now.

    Not the same question as "is it running". OpenSCAD stops answering while
    it renders — every menu click during a preview comes back -1719, "can't
    get menu bar 1, invalid index" — so anything that drives an application
    through a sequence has to wait between the steps that make it think.
    """
    script = (f'tell application "System Events" to tell process "{name}" '
              f'to get name of menu bar item 1 of menu bar 1')
    try:
        return _osa(script, timeout=10.0).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def wait_responsive(name: str, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app_responsive(name):
            return True
        time.sleep(0.5)
    return False


def menu_checked(app: str, menu: str, item: str) -> Optional[bool]:
    """Whether a checkable menu item is ticked. None if it cannot be read."""
    script = (f'tell application "System Events" to tell process "{app}" to get '
              f'value of attribute "AXMenuItemMarkChar" of menu item "{item}" '
              f'of menu 1 of menu bar item "{menu}" of menu bar 1')
    try:
        proc = _osa(script)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() not in ("", "missing value")


def click_menu(app: str, menu: str, item: str) -> str:
    """Click one menu item by name. Needs Accessibility.

    Menus rather than keyboard shortcuts: the item name is stable, while the
    shortcut is whatever that application happens to bind, and several of the
    ones wanted here — OpenSCAD's View All, Zoom In — are easier to name than
    to guess.
    """
    script = (f'tell application "System Events" to tell process "{app}" to click '
              f'menu item "{item}" of menu 1 of menu bar item "{menu}" of menu bar 1')
    try:
        proc = _osa(script)
    except subprocess.TimeoutExpired as exc:
        raise ControlError(f"{app} did not respond to {menu} > {item}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise ControlError(detail[-1][:160] if detail else f"{menu} > {item} failed")
    return f"{menu} > {item}"


def set_menu_checked(app: str, menu: str, item: str, want: bool) -> str:
    """Drive a checkable menu item to a state rather than toggling it.

    Toggling is wrong for anything run more than once: "hide the editor" would
    show it again on the second pass.
    """
    current = menu_checked(app, menu, item)
    if current is None or current == want:
        return f"{item} already {'on' if want else 'off'}"
    return click_menu(app, menu, item)


def _app_is_running(name: str) -> bool:
    """Whether an application is already up, without needing Accessibility."""
    script = (f'tell application "System Events" to '
              f'(name of processes) contains "{name}"')
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return proc.stdout.strip() == "true"


def search_web(query: str, engine: str = "google") -> str:
    """Search the web in the default browser."""
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
    """Titles and URLs of every tab in the front browser window."""
    browser = _browser_name()
    if browser in ("Google Chrome", "Brave Browser", "Arc"):
        script = (f'tell application "{browser}" to get {{title, URL}} of tabs of front window')
    elif browser == "Safari":
        script = 'tell application "Safari" to get {name, URL} of tabs of front window'
    else:
        return []
    try:
        raw = osascript(script)
    except ControlError:
        return []
    parts = [p.strip() for p in raw.split(", ")]
    half = len(parts) // 2
    return [{"title": t, "url": u} for t, u in zip(parts[:half], parts[half:])]


def browser_close_tab() -> str:
    press_key("cmd+w")
    return "tab closed"


def browser_new_tab() -> str:
    press_key("cmd+t")
    return "new tab"


def browser_select_tab(index: int) -> str:
    """Focus tab 1-8 in the front browser window (9 selects the last)."""
    press_key(f"cmd+{max(1, min(int(index), 9))}")
    return f"tab {index}"


# --------------------------------------------------------------- system state

def set_volume(level: int) -> str:
    """Set output volume, 0-100."""
    level = max(0, min(int(level), 100))
    osascript(f"set volume output volume {level}")
    return f"volume {level}"


def get_volume() -> int:
    return int(osascript("output volume of (get volume settings)"))


def volume_step(delta: int = 10) -> str:
    """Nudge the output volume by a relative amount."""
    return set_volume(get_volume() + int(delta))


def set_mute(muted: bool = True) -> str:
    osascript(f"set volume output muted {'true' if muted else 'false'}")
    return "muted" if muted else "unmuted"


def set_brightness(direction: str = "up", steps: int = 2) -> str:
    """Nudge display brightness ('up' or 'down')."""
    key = "brightnessup" if direction == "up" else "brightnessdown"
    for _ in range(max(1, int(steps))):
        press_key(key)
    return f"brightness {direction}"


def screenshot(path: Optional[str] = None, interactive: bool = False) -> str:
    """Capture the screen to ~/Desktop (or a given path)."""
    import pathlib
    if path is None:
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        path = str(pathlib.Path.home() / "Desktop" / f"JARVIS_{stamp}.png")
    args = ["screencapture", "-x"] + (["-i"] if interactive else []) + [path]
    subprocess.run(args, check=False)
    return f"captured {path}"


def lock_screen() -> str:
    press_key("ctrl+cmd+q")
    return "screen locked"


def sleep_display() -> str:
    subprocess.run(["pmset", "displaysleepnow"], check=False)
    return "display asleep"


def notify(text: str, title: str = "J.A.R.V.I.S.") -> str:
    osascript(f'display notification "{_as_escape(text)}" with title "{_as_escape(title)}"')
    return "notified"


def speak(text: str, voice: str = "Daniel", rate: int = 190) -> str:
    """Fallback text-to-speech through macOS `say` (the HUD normally does this)."""
    subprocess.Popen(["say", "-v", voice, "-r", str(rate), text])
    return "speaking"


def clipboard_get() -> str:
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout


def clipboard_set(text: str) -> str:
    subprocess.run(["pbcopy"], input=text, text=True, check=False)
    return "copied to clipboard"


def run_shortcut(name: str, text_input: Optional[str] = None) -> str:
    """Run a macOS Shortcut by name — the general escape hatch for custom actions."""
    if not shutil.which("shortcuts"):
        raise ControlError("the `shortcuts` CLI is unavailable")
    args = ["shortcuts", "run", name]
    proc = subprocess.run(args, input=text_input or "", capture_output=True, text=True)
    if proc.returncode != 0:
        raise ControlError(proc.stderr.strip() or f"shortcut {name!r} failed")
    return proc.stdout.strip() or f"ran shortcut {name}"


def run_shell(command: str, timeout: float = 20.0) -> str:
    """Run a shell command. Disabled unless safety.allow_shell is true."""
    proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or proc.stderr).strip()
    return out[:4000] or f"exit {proc.returncode}"


def system_status() -> Dict[str, object]:
    """Telemetry for the HUD readouts."""
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
        disk = psutil.disk_usage("/")
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
        raw = subprocess.run(["networksetup", "-getairportnetwork", "en0"],
                             capture_output=True, text=True, timeout=4).stdout.strip()
        # "Current Wi-Fi Network: <ssid>" when joined, a sentence when not.
        status["wifi"] = raw.split(": ", 1)[1] if ": " in raw else "not joined"
    except Exception:
        status["wifi"] = "unknown"
    return status
