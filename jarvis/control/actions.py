"""The action registry.

One table of everything J.A.R.V.I.S. can do. The same entries are used three
ways: as tools offered to the language model, as targets for gesture bindings,
and as the command palette the HUD displays. Adding a capability means adding
one `@action` here — the model, the gestures and the HUD all pick it up.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import macos

Schema = Dict[str, Any]


@dataclass
class Action:
    name: str
    fn: Callable[..., Any]
    description: str
    params: Schema = field(default_factory=lambda: {"type": "object", "properties": {}})
    confirm: bool = False
    category: str = "system"
    speak_result: bool = False

    def tool_spec(self) -> Schema:
        """Anthropic tool-use shape (also convertible to OpenAI's)."""
        return {"name": self.name, "description": self.description, "input_schema": self.params}


REGISTRY: Dict[str, Action] = {}


def action(name: str, description: str, params: Optional[Schema] = None,
           confirm: bool = False, category: str = "system",
           speak_result: bool = False) -> Callable:
    def wrap(fn: Callable) -> Callable:
        REGISTRY[name] = Action(
            name=name, fn=fn, description=description,
            params=params or {"type": "object", "properties": {}},
            confirm=confirm, category=category, speak_result=speak_result,
        )
        return fn
    return wrap


def _obj(**props: Schema) -> Schema:
    required = [k for k, v in props.items() if v.pop("_required", False)]
    return {"type": "object", "properties": props, "required": required}


def _str(desc: str, required: bool = True, **extra: Any) -> Schema:
    return {"type": "string", "description": desc, "_required": required, **extra}


def _int(desc: str, required: bool = True, **extra: Any) -> Schema:
    return {"type": "integer", "description": desc, "_required": required, **extra}


# ------------------------------------------------------------------ app control

action("open_app", "Launch or focus a Mac application by name, e.g. 'Safari', 'Spotify', 'Visual Studio Code'.",
       _obj(name=_str("Application name as a person would say it")),
       category="apps")(macos.open_app)

action("quit_app", "Quit a running application entirely.",
       _obj(name=_str("Application name")), confirm=True, category="apps")(macos.quit_app)

action("focus_app", "Bring an already-running application to the front.",
       _obj(name=_str("Application name")), category="apps")(macos.activate_app)

action("hide_app", "Hide an application's windows without quitting it.",
       _obj(name=_str("Application name; omit for the front app", required=False)),
       category="apps")(macos.hide_app)

action("list_apps", "List the applications currently running.",
       category="apps", speak_result=True)(macos.list_apps)


# ------------------------------------------------------------------- windows

action("close_window", "Close the frontmost window.", category="windows")(macos.close_window)
action("minimize_window", "Minimise the frontmost window.", category="windows")(macos.minimize_window)
action("fullscreen_window", "Toggle fullscreen on the front window.", category="windows")(macos.fullscreen_window)

action("snap_window", "Move the front window to a screen position.",
       _obj(where=_str("One of: left, right, top, bottom, full, center",
                       enum=["left", "right", "top", "bottom", "full", "center"]),
            app=_str("Application to affect; omit for the front app", required=False)),
       category="windows")(macos.snap_window)

action("list_windows", "List every open window with its app, title and geometry.",
       category="windows")(macos.list_windows)

action("mission_control", "Show Mission Control (all windows and spaces).",
       category="windows")(macos.mission_control)
action("show_desktop", "Reveal the desktop.", category="windows")(macos.show_desktop)
action("app_switcher", "Show the application switcher.", category="windows")(macos.app_switcher)

action("switch_space", "Switch to a numbered desktop/space.",
       _obj(index=_int("Space number, 1-9")), category="windows")(macos.switch_space)


# ------------------------------------------------------------------- browser

action("open_file", "Open a file on disk, optionally in a named application.",
       _obj(path=_str("Full path to the file"),
            app=_str("Application to open it with", required=False)),
       category="apps")(macos.open_file)

action("open_url", "Open a web page in the browser.",
       _obj(url=_str("Full URL or bare domain")), category="web")(macos.open_url)

action("search_web", "Search the web and show the results.",
       _obj(query=_str("What to search for"),
            engine=_str("google, duckduckgo, youtube, wikipedia or maps", required=False,
                        enum=["google", "duckduckgo", "youtube", "wikipedia", "maps"])),
       category="web")(macos.search_web)

action("browser_tabs", "List the tabs open in the front browser window.",
       category="web", speak_result=True)(macos.browser_tabs)
action("new_tab", "Open a new browser tab.", category="web")(macos.browser_new_tab)
action("close_tab", "Close the current browser tab.", category="web")(macos.browser_close_tab)
action("select_tab", "Switch to a numbered browser tab.",
       _obj(index=_int("Tab number, 1-9")), category="web")(macos.browser_select_tab)


# -------------------------------------------------------------- input / media

action("type_text", "Type text into the frontmost application.",
       _obj(text=_str("Exact text to type")), category="input")(macos.type_text)

action("press_key", "Press a keyboard shortcut, e.g. 'cmd+s', 'cmd+shift+t', 'escape'.",
       _obj(combo=_str("Key combination")), category="input")(macos.press_key)

action("click", "Click the mouse at its current position.",
       _obj(button=_str("left or right", required=False, enum=["left", "right"]),
            clicks=_int("1 for a single click, 2 to double-click", required=False)),
       category="input")(macos.click)

action("right_click", "Right-click (open the context menu) where the cursor is.",
       category="input")(lambda: macos.click(button="right"))

action("move_cursor", "Move the mouse cursor to a fraction of the screen (0-1 in each axis).",
       _obj(nx={"type": "number", "description": "Horizontal 0=left 1=right", "_required": True},
            ny={"type": "number", "description": "Vertical 0=top 1=bottom", "_required": True}),
       category="input")(macos.move_mouse_norm)

action("scroll", "Scroll the view under the cursor.",
       _obj(dy=_int("Positive scrolls up, negative down", required=False),
            dx=_int("Horizontal scroll", required=False)), category="input")(macos.scroll)

action("set_volume", "Set the system output volume.",
       _obj(level=_int("0 to 100")), category="media")(macos.set_volume)
action("volume_step", "Raise or lower the volume by a relative amount.",
       _obj(delta=_int("Positive to raise, negative to lower")),
       category="media")(macos.volume_step)
action("mute", "Mute or unmute system audio.",
       _obj(muted={"type": "boolean", "description": "true to mute", "_required": False}),
       category="media")(macos.set_mute)
action("play_pause", "Play or pause the current media.",
       category="media")(lambda: macos.media_key("play_pause"))
action("next_track", "Skip to the next track.", category="media")(lambda: macos.media_key("next_track"))
action("prev_track", "Go back to the previous track.", category="media")(lambda: macos.media_key("prev_track"))
action("set_brightness", "Raise or lower display brightness.",
       _obj(direction=_str("up or down", enum=["up", "down"]),
            steps=_int("How many notches", required=False)), category="media")(macos.set_brightness)


# --------------------------------------------------------------------- system

action("screenshot", "Capture the screen to the Desktop.",
       _obj(interactive={"type": "boolean", "description": "true to let the user drag a region",
                         "_required": False}), category="system")(macos.screenshot)

action("lock_screen", "Lock the Mac.", confirm=True, category="system")(macos.lock_screen)
action("sleep_display", "Put the display to sleep.", confirm=True, category="system")(macos.sleep_display)

action("system_status", "Read CPU, memory, disk, battery and network telemetry.",
       category="system", speak_result=True)(macos.system_status)

action("notify", "Post a macOS notification.",
       _obj(text=_str("Notification body"), title=_str("Title", required=False)),
       category="system")(macos.notify)

action("clipboard_read", "Read the clipboard contents.",
       category="system", speak_result=True)(macos.clipboard_get)
action("clipboard_write", "Put text on the clipboard.",
       _obj(text=_str("Text to copy")), category="system")(macos.clipboard_set)

action("run_shortcut", "Run a macOS Shortcut by name — use this for anything custom the user has built.",
       _obj(name=_str("Shortcut name"), text_input=_str("Optional input", required=False)),
       category="system", speak_result=True)(macos.run_shortcut)

action("run_shell", "Run a shell command. Disabled unless the user enabled it in config.",
       _obj(command=_str("Command line")), confirm=True, category="system",
       speak_result=True)(macos.run_shell)


# ------------------------------------------------------------------ dispatcher

class Dispatcher:
    """Runs actions off the event loop, applying the safety policy."""

    def __init__(self, config: dict, bus=None):
        self.config = config
        self.bus = bus
        safety = config.get("safety", {})
        self.confirm_actions = set(safety.get("confirm_actions", []))
        self.allow_shell = bool(safety.get("allow_shell", False))
        self.blocked_apps = {a.lower() for a in safety.get("blocked_apps", [])}
        self.pending: Optional[Dict[str, Any]] = None

    def needs_confirm(self, name: str) -> bool:
        act = REGISTRY.get(name)
        return bool(act and (act.confirm or name in self.confirm_actions))

    def tool_specs(self) -> List[Schema]:
        return [a.tool_spec() for a in REGISTRY.values()
                if not (a.name == "run_shell" and not self.allow_shell)]

    def describe(self) -> List[Dict[str, Any]]:
        """Registry summary for the HUD command palette."""
        return [{"name": a.name, "description": a.description, "category": a.category,
                 "confirm": self.needs_confirm(a.name)} for a in REGISTRY.values()]

    async def run(self, name: str, args: Optional[Dict[str, Any]] = None,
                  source: str = "voice", skip_confirm: bool = False) -> Dict[str, Any]:
        args = dict(args or {})
        act = REGISTRY.get(name)
        if act is None:
            return {"ok": False, "action": name, "error": f"no such action: {name}"}

        if name == "run_shell" and not self.allow_shell:
            return {"ok": False, "action": name,
                    "error": "shell execution is disabled in config (safety.allow_shell)"}
        if name in ("quit_app", "focus_app", "open_app"):
            target = str(args.get("name", "")).lower()
            if target in self.blocked_apps:
                return {"ok": False, "action": name, "error": f"{target} is blocked in config"}

        if self.needs_confirm(name) and not skip_confirm:
            self.pending = {"action": name, "args": args, "source": source}
            if self.bus:
                await self.bus.publish("confirm_required", action=name, args=args)
            return {"ok": False, "action": name, "pending": True,
                    "error": f"{name} needs confirmation"}

        # Drop only the arguments the callable cannot accept; a bad name from the
        # model should surface as an error rather than silently doing the wrong thing.
        try:
            signature = inspect.signature(act.fn)
            if not any(p.kind == p.VAR_KEYWORD for p in signature.parameters.values()):
                allowed = set(signature.parameters)
                unknown = set(args) - allowed
                if unknown:
                    return {"ok": False, "action": name,
                            "error": f"unexpected argument(s): {', '.join(sorted(unknown))}"}
        except (TypeError, ValueError):
            pass

        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: act.fn(**args))
            payload = {"ok": True, "action": name, "args": args, "result": result,
                       "source": source, "speak_result": act.speak_result}
        except Exception as exc:
            payload = {"ok": False, "action": name, "args": args,
                       "error": str(exc) or exc.__class__.__name__, "source": source}
        if self.bus:
            await self.bus.publish("action_result", **payload)
        return payload

    async def confirm_pending(self, accept: bool = True) -> Dict[str, Any]:
        pending, self.pending = self.pending, None
        if not pending:
            return {"ok": False, "error": "nothing awaiting confirmation"}
        if not accept:
            return {"ok": True, "action": pending["action"], "result": "cancelled",
                    "cancelled": True}
        return await self.run(pending["action"], pending["args"],
                              source=pending["source"], skip_confirm=True)
