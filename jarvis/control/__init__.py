"""Desktop control.

`desktop` is the backend for the machine this is running on. macOS, Windows
and Linux each provide the same functions, so the action registry, gestures
and CAD mode never have to ask which one it is; a capability a platform does
not have raises ControlError with a sentence saying so.
"""
import sys

if sys.platform == "darwin":
    from . import macos as desktop
elif sys.platform == "win32":
    from . import windows as desktop
else:
    from . import linux as desktop

PLATFORM = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
