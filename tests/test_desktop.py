"""The control backend for this operating system: it loads, it has every
function the action registry calls, and the read-only ones answer.

On a CI machine (CI=true) it also moves the mouse and reads it back, which is
the only proof that synthetic input reaches the desktop. It never does that on
a person's own machine.

    .venv/bin/python tests/test_desktop.py
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jarvis.control import PLATFORM, desktop  # noqa: E402
from jarvis.control.actions import REGISTRY  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  (' + detail + ')' if detail else ''}")
    if not ok:
        FAILURES.append(name)


print(f"\nbackend: {desktop.__name__} ({PLATFORM})")

# Every action points at a function of the backend that is actually loaded.
# (Wrappers defined in actions.py itself, like right_click, are not the backend's.)
missing = [name for name, a in REGISTRY.items()
           if getattr(a.fn, "__module__", "").startswith("jarvis.control")
           and a.fn.__module__ != "jarvis.control.actions"
           and not hasattr(desktop, a.fn.__name__)]
check("every registered action exists in this backend", not missing, ", ".join(missing))

# What the rest of the program calls on it directly.
needed = ["accessibility_trusted", "request_accessibility", "open_privacy_pane",
          "relay_available", "mouse_position", "move_mouse", "move_mouse_norm",
          "mouse_down", "mouse_up", "drag_to", "scroll", "screen_size", "click",
          "frontmost_app", "activate_app", "click_menu", "set_menu_checked",
          "wait_responsive", "system_status", "ControlError"]
absent = [n for n in needed if not hasattr(desktop, n)]
check("every function main, gestures and CAD mode call exists", not absent, ", ".join(absent))

status = desktop.system_status()
check("system_status reports cpu and memory", "cpu" in status and "mem_percent" in status,
      f"cpu {status.get('cpu')} mem {status.get('mem_percent')}% wifi {status.get('wifi')}")

try:
    apps = desktop.list_apps()
    check("list_apps answers", isinstance(apps, list), f"{len(apps)} apps")
except desktop.ControlError as exc:
    check("list_apps answers", True, f"unavailable: {exc}")

if PLATFORM != "macos":
    mods, key = desktop.parse_combo("cmd+shift+t")
    check("cmd means ctrl off the Mac", mods == ["ctrl", "shift"] and key == "t", f"{mods} {key}")

can_act = desktop.accessibility_trusted() or desktop.relay_available()
print(f"        can post input here: {can_act}")

if os.environ.get("CI") and can_act:
    w, h = desktop.screen_size()
    check("screen size is sane", w > 100 and h > 100, f"{w}x{h}")
    start = desktop.mouse_position()
    target = (w // 3, h // 3)
    desktop.move_mouse(*target)
    landed = desktop.mouse_position()
    check("the mouse goes where it is sent",
          abs(landed[0] - target[0]) < 4 and abs(landed[1] - target[1]) < 4,
          f"sent {target}, read {tuple(int(v) for v in landed)}")
    desktop.move_mouse(*start)
else:
    print("  SKIP  moving the mouse (only on CI)")

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
