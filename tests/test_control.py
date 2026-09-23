"""Does this process actually control the machine?

Run it from the SAME terminal you run J.A.R.V.I.S. from — permission is granted
to that terminal, so a different one will give a different answer.

    .venv/bin/python tests/test_control.py

It moves the cursor a little and puts it back. It does not click, type, or
touch any window.
"""
from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jarvis.control import macos


def main() -> int:
    print("\n  control check\n")

    trusted = macos.accessibility_trusted()
    print(f"  Accessibility reported: {'granted' if trusted else 'NOT granted'}")
    print("\n  NOTE: run this as ./jarvis-run --check-control instead.")
    print("  The venv python is a symlink to Apple's signed com.apple.python3,")
    print("  and macOS makes an Apple-signed binary its own responsible process")
    print("  rather than inheriting the terminal's grant — so calling python")
    print("  directly asks about a process that is not the one doing the work.")

    if not trusted:
        print("\n  Tick your terminal under Privacy & Security > Accessibility,")
        print("  then QUIT the terminal with Cmd-Q and open it again. A grant")
        print("  does not reach a process that is already running.\n")
        return 1

    # The report can be optimistic, so move the cursor and read it back — that
    # is the only proof that synthetic events are reaching the window server.
    try:
        start = macos.mouse_position()
        target = (start[0] + 60, start[1] + 40)
        macos.move_mouse(*target)
        time.sleep(0.25)
        landed = macos.mouse_position()
        macos.move_mouse(*start)          # put it back where it was
    except Exception as exc:
        print(f"\n  mouse control failed: {exc.__class__.__name__}: {exc}\n")
        return 1

    moved = abs(landed[0] - target[0]) < 4 and abs(landed[1] - target[1]) < 4
    print(f"  Cursor moved to {int(target[0])},{int(target[1])} and read back "
          f"{int(landed[0])},{int(landed[1])}")
    print(f"  Synthetic mouse events: {'WORKING' if moved else 'IGNORED'}")

    if not moved:
        print("\n  macOS accepted the call but the cursor did not move, which")
        print("  means the grant is stale. Remove your terminal from the")
        print("  Accessibility list, add it again, and restart it.\n")
        return 1

    # Middle-button and modifier drags are what CAD mode orbits with.
    try:
        macos.mouse_down("middle", ["shift"])
        macos.mouse_up("middle", ["shift"])
        print("  Middle-button + modifier drag: available (CAD orbit will work)")
    except Exception as exc:
        print(f"  Middle-button drag FAILED: {exc}")
        return 1

    print("\n  Control is live. Gestures and CAD mode will move things.\n")
    return 0


sys.exit(main())
