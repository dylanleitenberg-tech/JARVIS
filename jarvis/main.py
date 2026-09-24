"""J.A.R.V.I.S. — orchestrator.

Wires the pieces together and owns the routing decisions:

  the HUD (browser)  microphone, speech recognition, speech synthesis, visuals
  vision worker      hands, body, face -> landmarks
  gesture engine     landmarks -> gestures -> actions
  intents            utterance -> action, instantly, with no model call
  brain              anything intents did not catch -> model, with tool use
  dispatcher         the one place an action actually runs

Run it with `./jarvis` or `python -m jarvis.main`.
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import errno
import os
import json
import re
import signal
import subprocess
import sys
import time
import webbrowser
from typing import Any, Dict, Optional

from . import config as config_module
from .ai.brain import Brain
from .ai import intents
from .bus import Bus
from .control import macos
from .control.actions import Dispatcher
from .server import Server

BANNER = r"""
     ██ ▄▄▄       ██▀███   ██▒   █▓ ██▓  ██████
     ██ ████▄    ▓██ ▒ ██▒▓██░   █▒▓██▒▒██    ▒
     ██ ▒██  ▀█▄  ▓██ ░▄█ ▒ ▓██  █▒░▒██▒░ ▓██▄
  ▓▄▄ ██ ░██▄▄▄▄██ ▒██▀▀█▄    ▒██ █░░░██░  ▒   ██▒
   ▀▀▀  ▓█   ▓██▒░██▓ ▒██▒    ▒▀█░  ░██░▒██████▒▒
         ▒▒   ▓▒█░░ ▒▓ ░▒▓░    ░ ▐░  ░▓  ▒ ▒▓▒ ▒ ░
"""


class Jarvis:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.bus = Bus()
        self.dispatcher = Dispatcher(cfg, self.bus)
        self.brain = Brain(cfg, self.bus, self.dispatcher)
        self.brain.extra_tools = self._cad_tools
        self.brain.extra_run = self._cad_tool_run
        self.gestures = None
        self.vision = None
        self.server: Optional[Server] = None
        self.awake = True
        self.started = time.time()
        self.last_utterance = ""
        # Counters behind /api/health. "Is it reacting to me?" should have a
        # factual answer, not an impression.
        self.stat: Dict[str, Any] = {
            "vision_frames": 0, "hand_frames": 0, "last_hand_at": None,
            "voice_utterances": 0, "text_utterances": 0, "last_voice_at": None,
            "last_heard": "", "gestures_fired": 0, "last_gesture": None,
            "actions_ok": 0, "actions_failed": 0, "last_action": None,
            "mic": "unknown", "mic_detail": "",
        }
        self.quitting = False
        self._relaunches = 0
        self._hud_url = ""
        self._stop: Optional[asyncio.Event] = None
        from .models import ModelIndex
        self.models = ModelIndex(cfg.get("models", {}).get("roots", []),
                                 int(cfg.get("models", {}).get("max_files", 4000)))
        from .memory import Memory
        from .knowledge import Knowledge
        self.memory = Memory(config_module.ROOT / "logs" / "memory.json")
        self.knowledge = Knowledge(cfg.get("models", {}).get("roots", []))
        from .watch import Watcher
        self.watcher = Watcher(cfg)
        # A thing that has never worked has not been lost; these keep startup
        # from being announced as a series of failures.
        self._had_vision = False
        self._had_control = False
        self._brain_was_ready = False
        self._dirty_since = 0.0
        self._voices: List[str] = []       # what the browser reports it has
        self._voice: Optional[str] = None  # and which one it settled on
        intents.model_lookup = self.models.best
        # Live parameter editing of the .scad model on screen (cadedit.py).
        self.cad_edit = None
        intents.param_lookup = lambda name: self.cad_edit.find(name) if self.cad_edit else None
        intents.param_resolve = lambda name: self.cad_edit.resolve(name) if self.cad_edit else (None, [])
        self._cad_pending: Optional[Dict[str, Any]] = None     # a question JARVIS asked
        self._shown = None                                     # path of the model on screen
        self._captures: Dict[str, asyncio.Future] = {}         # in-flight look-at-it requests
        self.step_edit = None                                  # StepSession for a STEP on screen
        self._modify_lock = asyncio.Lock()
        self._modify_pending: Optional[Dict[str, Any]] = None  # an edit waiting on an answer
        self._smooth_pending: Optional[str] = None             # "smooth": render or reshape?
        self._cad_msg = ""

    # --------------------------------------------------------------- setup

    def hello(self) -> Dict[str, Any]:
        return {
            "config": {"speech": self.cfg["speech"], "hud": self.cfg["hud"],
                       "vision": {"enabled": self.cfg["vision"]["enabled"],
                                  "stream_feed": self.cfg["vision"]["stream_feed"]}},
            "actions": self.dispatcher.describe(),
            "ai": self.brain.info(),
            "gestures": {"armed": bool(self.gestures and self.gestures.armed),
                         "bindings": self.cfg["gestures"]["bindings"],
                         "enabled": bool(self.cfg["gestures"]["enabled"]),
                         "tuning": {k: self.cfg["gestures"].get(k) for k in self.TUNABLE},
                         "limits": {k: list(v) for k, v in self.TUNABLE.items()}},
            "cad": self.gestures.cad.status() if self.gestures else {"active": False},
            "accessibility": macos.accessibility_trusted(),
            "uptime": round(time.time() - self.started, 1),
        }

    async def run(self, args) -> None:
        loop = asyncio.get_running_loop()
        self.bus.bind_loop(loop)

        if self.cfg["gestures"]["enabled"] and self.cfg["vision"]["enabled"]:
            from .vision.gestures import GestureEngine
            self.gestures = GestureEngine(self.cfg, self.bus, self.dispatcher)
            self.bus.on("vision", self.gestures.on_vision)

        self.bus.on("vision", self._count_vision)
        self.bus.on("gesture", self._count_gesture)
        self.bus.on("action_result", self._count_action)

        self.server = Server(self.cfg, self.bus, self.on_hud_message,
                             get_jpeg=lambda: self.vision.latest_jpeg() if self.vision else None,
                             hello=self.hello, health=self.health,
                             models=self.models, edit_output=self._edit_output)
        try:
            url = await self.server.start()
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            port = self.cfg["server"]["port"]
            url = f"http://127.0.0.1:{port}/"
            # Clicking the app when it is already running should bring the
            # interface up, the way any other application behaves — not report
            # a port conflict at someone who just clicked an icon.
            if self.cfg["server"]["open_browser"] and not args.no_browser:
                print(f"  already running; showing the interface at {url}")
                self._hud_url = url
                self.open_hud(url)
                return
            holder = _port_holder(port)
            print(f"\n  Port {port} is already in use{holder}.")
            print("  J.A.R.V.I.S. is most likely already running.\n")
            print(f"    open the HUD   open -a 'Google Chrome' {url}")
            print( "    stop the old   pkill -f jarvis.main")
            print( "    or start here  ./jarvis-run --replace\n")
            return
        self._hud_url = url
        # A local model that is not running is the usual reason anything but a
        # fixed phrase goes unanswered. Start it now, not at the first question.
        if getattr(self.brain, "_local", False):
            await self.brain.ensure_local()

        # A backstop for the exits that do not run the shutdown path: an
        # unhandled exception, sys.exit from somewhere else, a parent that
        # goes away. It cannot help against SIGKILL, and nothing can.
        atexit.register(self.release_devices)

        print(BANNER)
        print(f"  HUD          {url}")
        print(f"  model        {self.brain.info()['backend']} / {self.brain.info()['detail']}")
        print(f"  vision       {'on' if self.cfg['vision']['enabled'] else 'off'}"
              f"   gestures {'on' if self.cfg['gestures']['enabled'] else 'off'}")
        trusted = macos.accessibility_trusted()
        note = ("granted" if trusted else
                "NOT GRANTED — keyboard, mouse and window control will do nothing")
        print(f"  accessibility {note}")
        print("\n  Speech runs in the browser tab; leave it focused and allow the microphone.")
        print("  Ctrl-C to shut down.\n")

        if self.cfg["vision"]["enabled"] and not args.no_vision:
            self.start_vision()

        if self.cfg["server"]["open_browser"] and not args.no_browser:
            self.open_hud(url)

        telemetry = asyncio.create_task(self.telemetry_loop())
        axwatch = asyncio.create_task(self.accessibility_watch())
        # Runs whether or not the HUD is meant to persist: with persist_hud on
        # it reopens the window, with it off it shuts down when the window
        # goes. Only skipped when there is no HUD to follow in the first place.
        watchdog = None
        if self.cfg["server"]["open_browser"] and not args.no_browser:
            watchdog = asyncio.create_task(self.hud_watchdog())
        stop = asyncio.Event()
        self._stop = stop
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        try:
            await stop.wait()
        finally:
            print("\n  standing down…")
            self.quitting = True
            for task in (telemetry, axwatch, watchdog):
                if task is None:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # Order matters. The page is told to let go of the microphone
            # first, then its window is closed, then the camera is released —
            # and each one is waited for. Setting a flag and exiting left both
            # devices live for as long as the OS took to notice.
            self.release_devices()
            await self.server.stop()

    def release_devices(self) -> None:
        """Give the camera and the microphone back. Safe to call twice.

        This is the answer to "why is the camera still on": stopping is not
        the same as having stopped. The vision thread holds the capture device
        and only releases it in its own `finally`, so the process has to wait
        for that to run rather than setting a flag and exiting.
        """
        if getattr(self, "_released", False):
            return
        self._released = True
        self.close_hud()                     # Chrome owns the microphone
        if self.vision:
            self.vision.stop(join=True)      # the worker owns the camera
        self._stop_bundle_parent()           # and the app bundle above us

    def _stop_bundle_parent(self) -> None:
        """Take JARVIS.app down with the assistant it started.

        The bundle forks this process and then serves the control relay for as
        long as it lives, which is by design — it is the signed binary the
        Accessibility grant is attached to. But the version installed in
        /Applications ignores SIGCHLD, so when the assistant exits it stays
        resident, serving nothing, holding the port and showing up as an
        application that will not stop running.

        Fixing that properly means rebuilding the bundle, and rebuilding it
        revokes all three TCC grants. So the child does it instead: on the way
        out it asks its parent to stop, having first checked that the parent
        really is the launcher. Nothing is signalled unless both are true, so
        this can never reach a terminal that started J.A.R.V.I.S. by hand.
        """
        if os.environ.get("JARVIS_BUNDLED") != "1":
            return
        parent = os.getppid()
        if parent <= 1:
            return                           # already orphaned; nothing to ask
        try:
            proc = subprocess.run(["ps", "-o", "args=", "-p", str(parent)],
                                  capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return
        if "JARVIS.app/Contents/MacOS" not in proc.stdout:
            return
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(parent, signal.SIGTERM)
        # The installed launcher unlinks its socket only on a normal return,
        # so a signalled one leaves it behind for the next run to trip over.
        with contextlib.suppress(OSError):
            sock = os.environ.get("JARVIS_CONTROL_SOCK")
            if sock:
                os.unlink(sock)

    def start_vision(self) -> None:
        from .vision.tracker import VisionWorker
        self.vision = VisionWorker(self.cfg, self.bus)
        self.vision.start()

    def _seed_chrome_permissions(self, profile: "pathlib.Path", url: str) -> None:
        """Pre-grant the microphone for our own URL in our own Chrome profile.

        The HUD opens as an `--app` window, which has no address bar — so if the
        microphone prompt is ever dismissed or blocked, there is no padlock icon
        to go back and undo it with. Writing the permission into the profile
        before Chrome starts avoids that dead end entirely. This touches only the
        dedicated profile J.A.R.V.I.S. created, never the user's own Chrome.
        """
        import json as _json
        import pathlib as _pathlib

        origin = url.rstrip("/")
        default = profile / "Default"
        prefs_path = default / "Preferences"
        try:
            default.mkdir(parents=True, exist_ok=True)
            prefs = {}
            if prefs_path.exists():
                try:
                    prefs = _json.loads(prefs_path.read_text())
                except _json.JSONDecodeError:
                    prefs = {}
            exceptions = (prefs.setdefault("profile", {})
                               .setdefault("content_settings", {})
                               .setdefault("exceptions", {}))
            for kind in ("media_stream_mic", "media_stream_camera"):
                exceptions.setdefault(kind, {})[f"{origin},*"] = {"setting": 1}
            prefs_path.write_text(_json.dumps(prefs))
        except OSError as exc:
            print(f"  (could not pre-grant the microphone: {exc})")

    def open_hud(self, url: str) -> None:
        """Prefer a dedicated Chrome window in app mode: no tabs, no chrome, no URL bar."""
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        import pathlib
        profile = config_module.ROOT / ".chrome-profile"
        if pathlib.Path(chrome).exists():
            self._seed_chrome_permissions(profile, url)
            flags = [chrome, f"--app={url}",
                     "--user-data-dir=" + str(profile),
                     "--autoplay-policy=no-user-gesture-required",
                     "--disable-features=TranslateUI",
                     "--no-first-run", "--no-default-browser-check"]
            if self.cfg["server"].get("kiosk"):
                flags.append("--start-fullscreen")
            # Keep the handle. Thrown away, this Chrome outlives J.A.R.V.I.S.
            # and keeps the microphone open — the window is gone, the page is
            # still listening, and the only sign is the menu-bar indicator.
            self._hud_proc = subprocess.Popen(
                flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(url)

    def close_hud(self, timeout: float = 4.0) -> None:
        """Close the HUD's own Chrome, and with it the microphone.

        Only ever the instance J.A.R.V.I.S. launched: it runs on a dedicated
        user-data-dir, so this cannot reach the browser the user is working
        in. Asks first, and insists only if asking does not work.
        """
        proc, self._hud_proc = getattr(self, "_hud_proc", None), None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=2.0)

    # -------------------------------------------------------- HUD watchdog

    async def hud_watchdog(self) -> None:
        """Follow the HUD window: reopen it, or shut down with it.

        Closing the interface has to be a way out. Without this, closing the
        window left the whole assistant running headless — listening on its
        port, holding the camera, with nothing on screen to say so, and no
        obvious way to stop it short of finding the process.

        A reload also drops the websocket, so a grace period tells "reloading"
        apart from "closed". Which of the two things happens after that is
        `persist_hud`: on, the window is reopened, capped, because fighting
        the user is the wrong answer; off, this is a close and the assistant
        goes with it. A deliberate power-down sets `self.quitting` and is
        never second-guessed.
        """
        cfg = self.cfg["server"]
        grace = float(cfg.get("relaunch_after", 8.0))
        cap = int(cfg.get("max_relaunches", 6))
        persist = bool(cfg.get("persist_hud"))
        gone_since: Optional[float] = None
        seen_client = False

        while not self.quitting:
            await asyncio.sleep(2.0)
            if self.quitting or not self.server:
                break
            if self.server.clients:
                gone_since = None
                seen_client = True
                continue
            # Nothing has connected yet — the browser is still starting. This
            # is not a closed window, and must not be read as one.
            if not seen_client:
                continue
            now = time.time()
            if gone_since is None:
                gone_since = now
                continue
            if now - gone_since < grace:
                continue

            if not persist:
                await self.bus.publish("log", level="warn",
                                       text="HUD closed — standing down")
                await self.power_down()
                return
            if self._relaunches >= cap:
                await self.bus.publish("log", level="warn",
                                       text="HUD closed repeatedly; leaving it shut")
                break
            self._relaunches += 1
            gone_since = None
            await self.bus.publish("log", level="warn", text="HUD closed — reopening")
            self.open_hud(self._hud_url)

    async def power_down(self) -> None:
        """The deliberate way out: stop the watchdog, then stop the process."""
        self.quitting = True
        await self.bus.publish("power_down")
        await asyncio.sleep(1.2)          # let the HUD speak its last line
        if self._stop is not None:
            self._stop.set()

    # ------------------------------------------------------------ diagnostics

    def vision_error(self) -> str:
        if self.vision is not None and self.vision.error:
            self._last_vision_error = self.vision.error
        return getattr(self, "_last_vision_error", "")

    async def _count_vision(self, event: dict) -> None:
        self.stat["vision_frames"] += 1
        if event.get("hands"):
            self.stat["hand_frames"] += 1
            self.stat["last_hand_at"] = time.time()

    async def _count_gesture(self, event: dict) -> None:
        if event.get("name") in ("press",):      # half of a click, not a gesture
            return
        self.stat["gestures_fired"] += 1
        self.stat["last_gesture"] = event.get("name")

    async def _count_action(self, event: dict) -> None:
        if event.get("pending"):
            return
        self.stat["actions_ok" if event.get("ok") else "actions_failed"] += 1
        self.stat["last_action"] = event.get("action")

    def health(self) -> Dict[str, Any]:
        """A straight answer to 'is it seeing me, hearing me, and able to act?'"""
        now = time.time()
        stat = dict(self.stat)
        seen = stat["last_hand_at"]
        heard = stat["last_voice_at"]
        trusted = macos.accessibility_trusted()

        def ago(t):
            return None if t is None else round(now - t, 1)

        problems = []
        if not (self.vision and self.vision.running):
            why = self.vision_error()
            problems.append(
                f"camera is not running — {why}" if why else
                "camera is not running: grant Camera to your terminal app in "
                "System Settings > Privacy & Security > Camera, then restart it")
        elif stat["hand_frames"] == 0:
            problems.append("camera is running but has never seen a hand — "
                            "hold one up, 40-70 cm from the lens")
        if stat["mic"] == "unknown":
            problems.append("the HUD has not reported a microphone — open the "
                            "HUD page and click it once, then allow the mic in Chrome")
        elif stat["mic"] == "macos-denied":
            problems.append("macOS is refusing Chrome the microphone. System "
                            "Settings > Privacy & Security > Microphone > turn on "
                            "Google Chrome, then fully quit and reopen Chrome")
        elif stat["mic"] != "live":
            problems.append(f"microphone is {stat['mic']}"
                            + (f" ({stat['mic_detail']})" if stat["mic_detail"] else ""))
        elif stat["voice_utterances"] == 0:
            problems.append("microphone is live but nothing has been transcribed — "
                            'say "Jarvis" and then a command')
        if not (trusted or macos.relay_available()):
            problems.append("Accessibility is not granted, so every gesture and "
                            "command that moves the mouse, presses a key or touches "
                            "a window will do nothing. Tick JARVIS under Privacy & "
                            "Security > Accessibility, then restart the app")

        return {
            "eyes": {
                "camera": bool(self.vision and self.vision.running),
                "error": self.vision_error(),
                "fps": self.vision.fps if self.vision else 0.0,
                "frames": stat["vision_frames"],
                "frames_with_hands": stat["hand_frames"],
                "seconds_since_hand": ago(seen),
            },
            "ears": {
                "mic": stat["mic"],
                "mic_detail": stat["mic_detail"],
                "voice_commands": stat["voice_utterances"],
                "typed_commands": stat["text_utterances"],
                "seconds_since_voice": ago(heard),
                "last_heard": stat["last_heard"],
                # What he is actually speaking with, as opposed to what the
                # config asks for. The two diverge silently whenever a voice
                # is named that this browser does not have.
                "voice_wanted": self.cfg["speech"].get("voice_hint") or "(best available)",
                "voice_using": getattr(self, "_voice", None),
                "voices_available": len(getattr(self, "_voices", [])),
            },
            "hands": {
                "armed": bool(self.gestures and self.gestures.armed),
                "gestures_fired": stat["gestures_fired"],
                "last_gesture": stat["last_gesture"],
            },
            "control": {
                # What matters is whether anything can actually move, which is
                # true when this process is trusted OR the app relay is up.
                "accessibility": trusted or macos.relay_available(),
                "via": ("app relay" if macos.relay_available()
                        else "direct" if trusted else "none"),
                "actions_ok": stat["actions_ok"],
                "actions_failed": stat["actions_failed"],
                "last_action": stat["last_action"],
            },
            "brain": self.brain.info(),
            "uptime_s": round(now - self.started, 1),
            "problems": problems or ["none — everything is wired up"],
        }

    # ------------------------------------------------------------ telemetry

    async def accessibility_watch(self) -> None:
        """Announce the moment control permission becomes available.

        Toggling the checkbox does not normally reach a process that is already
        running, so this both reports the flip when it happens and says plainly
        what to do when it does not.
        """
        was = macos.accessibility_trusted()
        told = False
        while not self.quitting:
            await asyncio.sleep(4.0)
            now = macos.accessibility_trusted()
            if now != was:
                was = now
                await self.bus.publish("accessibility", granted=now)
                await self.say("Control permissions granted. I have the machine."
                               if now else "Control permissions revoked.")
            elif not now and not told and time.time() - self.started > 25:
                told = True
                await self.bus.publish("log", level="warn", text=(
                    "Accessibility is still off for this process. Tick your "
                    "terminal in Privacy & Security > Accessibility, then QUIT "
                    "the terminal with Cmd-Q and start J.A.R.V.I.S. again — a "
                    "grant does not reach a process that is already running."))

    async def telemetry_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                status = await loop.run_in_executor(None, macos.system_status)
                status["vision_fps"] = self.vision.fps if self.vision else 0.0
                status["vision_online"] = bool(self.vision and self.vision.running)
                status["armed"] = bool(self.gestures and self.gestures.armed)
                status["can_control"] = (macos.accessibility_trusted()
                                         or macos.relay_available())
                status["awake"] = self.awake
                await self.bus.publish("telemetry", **status)
                await self._maybe_speak_up(status)
            except Exception as exc:
                await self.bus.publish("log", level="warn", text=f"telemetry: {exc}")
            await asyncio.sleep(3.0)

    # -------------------------------------------------------- HUD messages

    async def on_hud_message(self, msg: dict, ws) -> None:
        kind = msg.get("type")

        if kind == "voice_heard":
            names = [str(n) for n in (msg.get("names") or [])]
            await self.bus.publish("log", level="info",
                                   text="voices: " + ", ".join(names))
            return

        if kind == "voice_set":
            name = msg.get("name")
            if not name:
                await self.say(f"I have no voice called {msg.get('asked')}.")
                return
            # Kept across restarts, or choosing one is a ritual you repeat
            # every launch.
            config_module.save_patch({"speech": {"voice_hint": name}})
            self.cfg["speech"]["voice_hint"] = name
            self.memory.remember(f"Use the '{name}' speech voice.", source="user")
            return

        if kind == "capture":
            waiter = self._captures.pop(str(msg.get("id", "")), None)
            if waiter is not None and not waiter.done():
                waiter.set_result(list(msg.get("shots") or []))
            return

        if kind == "utterance":
            await self.handle_utterance(str(msg.get("text", "")), msg.get("source", "voice"))
        elif kind == "action":
            await self.dispatcher.run(str(msg.get("name")), msg.get("args") or {},
                                      source="hud")
        elif kind == "confirm":
            result = await self.dispatcher.confirm_pending(bool(msg.get("accept", True)))
            if result.get("cancelled"):
                await self.say("Cancelled.")
            elif result.get("ok"):
                await self.say("Done.")
        elif kind == "wake":
            self.awake = True
            await self.bus.publish("awake", awake=True)
        elif kind == "sleep":
            self.awake = False
            await self.bus.publish("awake", awake=False)
        elif kind == "cad":
            if self.gestures:
                if msg.get("active"):
                    if not self.gestures.armed:
                        await self.gestures.set_armed(True, why="cad")
                    await self.gestures.cad.enter(why="hud")
                else:
                    await self.gestures.cad.leave(why="hud")
        elif kind == "arm_gestures":
            if self.gestures:
                await self.gestures.set_armed(bool(msg.get("armed", True)), why="hud")
        elif kind == "vision":
            # Start or stop the camera on request — it is never opened silently.
            if msg.get("on") and not (self.vision and self.vision.running):
                self.start_vision()
            elif not msg.get("on") and self.vision:
                self.vision.stop()
                self.vision = None
        elif kind == "reset":
            self.brain.reset()
            await self.bus.publish("log", level="info", text="context cleared")
        elif kind == "gesture_tune":
            await self.tune_gestures(msg.get("patch") or {}, bool(msg.get("save")))
        elif kind == "rebind":
            await self.rebind(str(msg.get("gesture", "")), msg.get("action"),
                              bool(msg.get("save")))
        elif kind == "quit":
            await self.say("Powering down. Good day, Sir.")
            asyncio.create_task(self.power_down())
        elif kind == "mic":
            self.stat["mic"] = str(msg.get("state", "unknown"))
            if msg.get("detail"):
                self.stat["mic_detail"] = str(msg["detail"])[:200]
                await self.bus.publish("log", level="warn",
                                       text=f"microphone: {msg['detail']}")
        elif kind == "model_view":
            if self.gestures:
                self.gestures.viewer_open = bool(msg.get("open"))
        elif kind == "model_loaded":
            await self.start_edit(str(msg.get("path", "")))
        elif kind == "cad_param":
            s = self.cad_edit
            name = str(msg.get("name", ""))
            if s and name in s.by_name:
                try:
                    s.set(name, msg.get("value"))
                except (TypeError, ValueError):
                    return
                self._schedule_rebuild(name)
        elif kind == "ready":
            self._voices = [str(v) for v in (msg.get("voices") or [])]
            self._voice = msg.get("voice")
            want = str(self.cfg["speech"].get("voice_hint") or "").strip()
            if want and self._voices and not any(
                    want.lower() in v.lower() for v in self._voices):
                await self.bus.publish(
                    "log", level="warn",
                    text=f"no voice called '{want}' in this browser; using {self._voice}")
            await self.greet()
        elif kind == "ping":
            await ws.send_json({"type": "pong", "t": time.time()})

    # Bounds so a slider cannot put the recogniser into a state where it fires
    # constantly or never fires at all.
    TUNABLE = {
        "pinch_on": (0.08, 0.70), "pinch_off": (0.10, 0.95),
        "cooldown": (0.10, 3.00), "arm_hold": (0.20, 4.00),
        "min_hand_scale": (0.02, 0.30), "swipe_velocity": (0.40, 4.00),
        "cursor_smoothing": (0.05, 1.00), "disarm_after_idle": (5.0, 600.0),
    }

    async def tune_gestures(self, patch: Dict[str, Any], save: bool = False) -> None:
        """Apply gesture tuning live, and optionally persist it."""
        clean: Dict[str, Any] = {}
        for key, value in patch.items():
            if key not in self.TUNABLE:
                continue
            low, high = self.TUNABLE[key]
            try:
                clean[key] = max(low, min(float(value), high))
            except (TypeError, ValueError):
                continue
        if not clean:
            return
        # pinch_off must stay above pinch_on or the hysteresis inverts and a
        # pinch would latch on forever.
        on = clean.get("pinch_on", self.cfg["gestures"]["pinch_on"])
        off = clean.get("pinch_off", self.cfg["gestures"]["pinch_off"])
        if off <= on:
            clean["pinch_off"] = round(on + 0.05, 3)

        self.cfg["gestures"].update(clean)
        if self.gestures:
            self.gestures.cfg.update(clean)      # same dict, but be explicit
        if save:
            config_module.save_patch({"gestures": clean})
        await self.bus.publish("gesture_tuned", patch=clean, saved=save)

    async def rebind(self, gesture: str, action: Optional[str], save: bool = False) -> None:
        """Point a gesture at a different action, or at nothing."""
        bindings = self.cfg["gestures"]["bindings"]
        if gesture not in bindings:
            return
        from .control.actions import REGISTRY
        reserved = {"cursor", "drag", "zoom", "radial_menu", "confirm", "dismiss",
                    "next_app", "prev_app"}
        if action and action not in REGISTRY and action not in reserved:
            await self.bus.publish("log", level="warn",
                                   text=f"no such action: {action}")
            return
        bindings[gesture] = action or None
        if self.gestures:
            self.gestures.cfg["bindings"][gesture] = action or None
        if save:
            config_module.save_patch({"gestures": {"bindings": {gesture: action or None}}})
        await self.bus.publish("rebound", gesture=gesture, action=action, saved=save)

    async def greet(self) -> None:
        """Greet the first HUD of the session; stay quiet on reconnects."""
        if time.time() - getattr(self, "_greeted", 0.0) < 120:
            return
        self._greeted = time.time()
        hour = time.localtime().tm_hour
        part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        notes = []
        if not macos.accessibility_trusted():
            notes.append("I have no control permissions yet")
        if self.brain.status != "ready":
            notes.append("and no model is connected")
        tail = (", though " + " ".join(notes) + ".") if notes else ". All systems are nominal."
        await self.say(f"Good {part}, Sir{tail}")

    # ----------------------------------------------------------- utterances

    async def handle_utterance(self, text: str, source: str = "voice") -> None:
        text = text.strip()
        if not text:
            return
        self.last_utterance = text
        self.stat["last_heard"] = text
        if source == "voice":
            self.stat["voice_utterances"] += 1
            self.stat["last_voice_at"] = time.time()
        else:
            self.stat["text_utterances"] += 1
        await self.bus.publish("utterance", text=text, source=source)
        self._activity("heard", text=text, source=source,
                       shown=str(self._shown) if self._shown else None,
                       pending=bool(self._cad_pending or self._modify_pending or self._smooth_pending))
        # "Jarvis" on its own is the wake word arriving by itself; the request
        # follows. It must not answer (and so cancel) a question JARVIS asked.
        if not intents._ADDRESSED.sub("", text).strip(" ,.!?"):
            return

        # "Smooth" can mean two things; JARVIS asked which.
        if self._smooth_pending:
            pending, self._smooth_pending = self._smooth_pending, None
            low = text.lower()
            if re.search(r"\b(?:shape|surface|solid|geometry|reshape|replace|actual|real|cone|shell|model it|change it)\b", low):
                self._activity("route", to="modify", why="smooth answer", request=pending)
                self._spawn(self.modify_geometry(f"{pending} (the user means: change the geometry into a smooth surface)"), "geometry edit")
                return
            if re.search(r"\b(?:draw|render|look|display|shading|mesh|view|picture|visual)\b", low):
                self._activity("route", to="smooth", why="smooth answer")
                await self.smooth_model(speak=True)
                return

        # JARVIS asked "lens width, lens height or lens radius?" and this is the answer.
        if self._cad_pending and await self._answer_cad_question(text, source):
            return
        # Claude asked something about a geometry edit ("which end stays fixed?").
        if self._modify_pending:
            pending, self._modify_pending = self._modify_pending, None
            if time.time() - pending["at"] < 90 and not intents.match(text):
                self._activity("route", to="modify", why="answer to edit question", request=pending["request"])
                self._spawn(self.modify_geometry(f"{pending['request']} (answer to your question: {text})"), "geometry edit")
                return

        intent = intents.match(text)
        self._activity("intent", text=text, intent=intent[0] if intent else None,
                       args=intent[1] if intent else None)
        if intent is not None:
            action, args, ack = intent

            if action == "__say":
                await self.say(args["text"])
                return
            if action == "__arm_gestures":
                if self.gestures:
                    await self.gestures.set_armed(bool(args["armed"]), why="voice")
                    await self.say(ack)
                else:
                    await self.say("Gesture tracking is not running.")
                return
            if action == "__voice_audition":
                await self.say("Here are the voices I have. Say use, and the name.")
                await self.bus.publish("voice_audition", limit=6,
                                       sample="Good evening, Sir. All systems nominal.")
                return
            if action == "__voice_use":
                await self.bus.publish("voice_use", name=str(args.get("name", "")))
                return

            if action == "__open_source":
                query = str(args.get("query", "")).strip()
                if not await self.open_source(query, str(args.get("app", "")), source):
                    await self.say(f"I have no source file called {query}.")
                return

            if action == "__model":
                query = str(args.get("query", "")).strip()
                hit = self.models.best(query, prefer=args.get("prefer")) if query else None
                slow = bool(hit) and self.models.needs_build(str(hit["path"]))
                await self.bus.publish("show_model", model=hit, query=query)
                if hit and slow:
                    await self.say(f"{str(hit['name']).replace('_', ' ')}. "
                                   f"Building it from the source first; give me a moment.")
                elif hit:
                    await self.say(f"{str(hit['name']).replace('_', ' ')}. "
                                   f"Pinch to turn it.")
                else:
                    await self.say("Model library.")
                return
            if action == "__cad":
                if not self.gestures:
                    await self.say("Gesture tracking is not running.")
                    return
                want = bool(args["active"])
                if want:
                    if not self.gestures.armed:
                        await self.gestures.set_armed(True, why="cad")
                    await self.gestures.cad.enter(why="voice")
                    st = self.gestures.cad.status()
                    await self.say(f"CAD mode. {st['app'] or 'Unknown application'}, "
                                   f"{st['profile']} controls.")
                else:
                    await self.gestures.cad.leave(why="voice")
                    await self.say(ack)
                return
            if action == "__cad_fit":
                if self.gestures and self.gestures.cad.active:
                    await self.gestures.cad.fit()
                else:
                    await self.say("Not in CAD mode.")
                return
            if action == "__quit":
                await self.say(ack)
                asyncio.create_task(self.power_down())
                return
            if action == "__confirm" and not getattr(self.dispatcher, "pending", None) and \
                    self.brain.cmd_history and self.brain.cmd_history[-1]["reply"].rstrip().endswith("?"):
                # "Yes, do it" answers the question the model just asked, not a
                # confirmation gate: it goes back to the model with that context.
                reply = await self.brain.ask(text)
                await self.say(reply)
                return
            if action == "__confirm":
                result = await self.dispatcher.confirm_pending(bool(args["accept"]))
                if result.get("cancelled"):
                    await self.say("Cancelled.")
                elif result.get("ok"):
                    await self.say("Done.")
                elif result.get("error"):
                    await self.say("Nothing was waiting on you, Sir.")
                return

            if action == "__model_smooth":
                low = text.lower()
                editable = self.step_edit is not None or self.cad_edit is not None
                if editable and re.search(r"\b(?:instead of|as opposed to|rather than|not made of|out of)\b", low):
                    self._activity("route", to="modify", why="smooth + instead of")
                    self._spawn(self.modify_geometry(text), "geometry edit")
                    return
                names_a_part = not re.search(r"^(?:\W*(?:hey |ok |okay )?jarvis\W*)?(?:make |render |draw |show |turn )?"
                                             r"(?:it|this|that)?\s*(?:look )?(?:smooth|smoother|higher detail|high detail|"
                                             r"more detail|less faceted|finer)", low)
                if editable and names_a_part:
                    self._smooth_pending = text
                    self._activity("ask", what="smooth: render or reshape")
                    await self.say("Draw it smoother, or reshape it into a smooth surface?")
                    return
                await self.smooth_model(speak=True)
                return

            if action.startswith("__cad_") and action not in ("__cad_fit",):
                if await self.cad_action(action, args, source):
                    return

            if action.startswith("__"):
                # An unresolved internal intent: hand it to the model instead
                # of trying to run it as an action.
                reply = await self.brain.ask(text)
                await self.say(reply)
                return

            result = await self.dispatcher.run(action, args, source=source)
            if result.get("pending"):
                await self.say(f"{action.replace('_', ' ')}. Confirm?")
                return
            if not result.get("ok"):
                # "Open astrowilly" is not an application, and saying so is
                # useless when the file is sitting in one of the model roots.
                # Before reporting a failure, look for it there.
                if action in ("open_app", "focus_app") and \
                        await self.open_source(str(args.get("name", "")), "", source):
                    return
                await self.say(f"That failed. {result.get('error', '')}"[:180])
                return
            await self.say(ack or self.summarise(action, result.get("result")))
            return

        # "Make the nozzle longer" with a STEP on screen: nothing to edit, and
        # silence (or a model with no idea what is on screen) is the worst answer.
        if self._fixed_geometry_request(text):
            self._activity("route", to="modify" if self.step_edit else "explain", why="edit words on STEP/mesh")
            if self.step_edit is not None:
                self._spawn(self.modify_geometry(text), "geometry edit")
            else:
                await self.say(self._fixed_geometry_line())
            return

        # Nothing matched locally — hand it to the model.
        self._activity("route", to="brain", backend=self.brain.info().get("detail"))
        reply = await self.brain.ask(text)
        self._activity("brain_reply", reply=reply)
        await self.say(reply)

    # ------------------------------------------------------ background jobs

    def _spawn(self, coro, label: str):
        """Run a job in the background without losing it. A bare create_task is
        held only weakly, and an exception inside it is dropped with nothing
        said: a spoken edit that crashed simply never happened. Here the task
        is kept until it finishes, and a failure is logged and said aloud."""
        task = asyncio.create_task(coro)
        if not hasattr(self, "_jobs"):
            self._jobs = set()
        self._jobs.add(task)

        def done(t: asyncio.Task) -> None:
            self._jobs.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                self._activity("job_failed", job=label, error=f"{type(exc).__name__}: {exc}")
                asyncio.create_task(self.bus.publish("log", level="error",
                                                     text=f"{label} failed: {type(exc).__name__}: {exc}"))
                asyncio.create_task(self.say(f"That failed, Sir. {type(exc).__name__}."))
        task.add_done_callback(done)
        return task

    # --------------------------------------------------------- activity log

    def _activity(self, kind: str, **data: Any) -> None:
        """One JSON line per thing heard, routed, asked, run or edited, in
        logs/activity.log, so "it didn't do what I said" can be looked up
        afterwards instead of guessed at. Kept to the last ~2 MB."""
        try:
            import pathlib
            path = pathlib.Path(__file__).resolve().parents[1] / "logs" / "activity.log"
            path.parent.mkdir(exist_ok=True)
            if path.exists() and path.stat().st_size > 2_000_000:
                path.write_text(path.read_text()[-1_000_000:])
            with open(path, "a") as f:
                clip = {k: (v[:1500] if isinstance(v, str) else v) for k, v in data.items()}
                line = json.dumps(dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), kind=kind, **clip), default=str)
                if len(line) > 8000:          # still whole JSON, just shorter
                    line = json.dumps(dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), kind=kind,
                                           note="entry too long", keys=list(data)))
                f.write(line + "\n")
        except Exception:
            pass

    # ------------------------------------------------------- live CAD edits

    def _edit_output(self, key: str):
        if not (len(key) == 16 and all(c in "0123456789abcdef" for c in key)):
            return None
        for s in (self.cad_edit, self.step_edit):
            if s is not None:
                path = s.output(key)
                if path.exists():
                    return path
        return None

    async def start_edit(self, raw_path: str) -> None:
        """The viewer loaded a model. A .scad one becomes editable."""
        import pathlib
        from .cadedit import EditSession
        from .models import OPENSCAD
        path = self.models.resolve(raw_path) if raw_path else None
        self._shown = path
        # What is on screen decides which project's notes are worth offering,
        # and is worth remembering for "what was I looking at yesterday".
        if path is not None:
            hit = next((m for m in self.models.scan() if m["path"] == str(path)), None)
            self.knowledge.set_focus(str(hit["project"]) if hit else None)
            self.memory.note("model", f"looking at {path.stem.replace('_', ' ')}",
                             file=path.name, project=hit["project"] if hit else None)
            self.memory.save()
        else:
            self.knowledge.set_focus(None)
        if path is not None and path.suffix.lower() in (".step", ".stp"):
            self.cad_edit = None
            await self._start_step_edit(path)
            return
        self.step_edit = None
        if path is None or path.suffix.lower() != ".scad" or not pathlib.Path(OPENSCAD).exists():
            self.cad_edit = None
            await self.bus.publish("cad_params", path=None, params=[], dirty=False)
            return
        if self.cad_edit is None or self.cad_edit.path != path:
            try:
                self.cad_edit = await asyncio.to_thread(EditSession, path, OPENSCAD)
            except OSError as exc:
                self.cad_edit = None
                await self.bus.publish("log", level="warn", text=f"cannot edit {path.name}: {exc}")
                return
        s = self.cad_edit
        await self.bus.publish("cad_params", path=str(path), name=path.stem,
                               params=s.state(), dirty=s.dirty)

    async def _start_step_edit(self, path) -> None:
        from .stepedit import StepSession
        python = self.models.step_python()
        if python is None:
            self.step_edit = None
            await self.bus.publish("cad_params", path=None, params=[], dirty=False)
            return
        if self.step_edit is None or self.step_edit.path != path:
            self.step_edit = StepSession(path, python)
            # Reading the part list takes a few seconds on a big assembly; do
            # it now so the first spoken edit does not wait on it.
            session = self.step_edit
            asyncio.create_task(asyncio.to_thread(self._warm_inventory, session))
        await self._publish_step_state()

    @staticmethod
    def _warm_inventory(session) -> None:
        try:
            session.inventory()
        except Exception:
            pass

    async def _publish_step_state(self) -> None:
        s = self.step_edit
        if s is None:
            return
        await self.bus.publish("cad_params", path=str(s.path), name=s.path.stem, params=[],
                               dirty=s.dirty, edits=[e["say"] or e["request"] for e in s.scripts],
                               mode="step")

    def _schedule_rebuild(self, changed: Optional[str] = None) -> None:
        self._spawn(self._rebuild(changed), "rebuild")

    async def _rebuild(self, changed: Optional[str] = None) -> None:
        if self.cad_edit is None and self.step_edit is not None:
            st = self.step_edit
            await self.bus.publish("cad_building", name=changed)
            out = await asyncio.to_thread(st.build)
            if st is not self.step_edit:
                return
            if out is None:
                await self.bus.publish("cad_failed", name=changed, error=st.last_error[-300:])
                return
            key = out.stem.rsplit("-", 1)[1]
            await self.bus.publish("model_update", url=f"/api/model_edit?key={key}",
                                   dirty=st.dirty, edits=[e["say"] or e["request"] for e in st.scripts])
            await self._publish_step_state()
            return
        s = self.cad_edit
        if s is None:
            return
        await self.bus.publish("cad_building", name=changed)
        started = time.time()
        out = await asyncio.to_thread(s.compile)
        if s is not self.cad_edit:
            return
        if out is None:
            if s.last_error:                  # superseded builds say nothing
                await self.bus.publish("cad_failed", name=changed, error=s.last_error[-300:])
                await self.bus.publish("log", level="warn",
                                       text=f"{s.path.name}: {s.last_error[-200:]}")
            return
        key = out.stem.rsplit("-", 1)[1]
        await self.bus.publish("model_update", url=f"/api/model_edit?key={key}",
                               params=s.state(), changed=changed, dirty=s.dirty,
                               seconds=round(time.time() - started, 2))

    @staticmethod
    def _say_value(p, value) -> str:
        if p.kind == "bool":
            return "on" if value else "off"
        if p.kind == "int":
            return str(int(value))
        return f"{float(value):.4g}"

    async def cad_action(self, action: str, args: Dict[str, Any], source: str,
                         speak: bool = True) -> bool:
        """Voice edits to the model on screen. Returns False to let the
        utterance fall through when there is nothing to edit. With speak=False
        (the model calling it as a tool) the line is kept in self._cad_msg for
        the model to report instead of being said twice."""
        async def tell(line: str) -> None:
            self._cad_msg = line
            if speak:
                await self.say(line)

        if action == "__cad_modify":
            self._spawn(self.modify_geometry(str(args.get("request", "")), speak=speak), "geometry edit")
            return True
        st = self.step_edit if self.cad_edit is None else None
        if st is not None and action in ("__cad_undo", "__cad_reset", "__cad_save"):
            if action == "__cad_undo":
                if st.undo():
                    self._schedule_rebuild()
                    await tell("Undone.")
                else:
                    await tell("Nothing to undo.")
            elif action == "__cad_reset":
                st.reset()
                self._schedule_rebuild()
                await tell("Back to the original export.")
            else:
                if not st.dirty:
                    await tell("Nothing has changed.")
                    return True
                await tell("Saving a new STEP file. This takes a few seconds.")
                try:
                    dest = await asyncio.to_thread(st.save)
                except Exception as exc:
                    await tell(f"Not saved. {exc}"[:160])
                    return True
                await self.bus.publish("log", level="info", text=f"saved {dest}")
                await tell(f"Saved as {dest.name}, next to the original. The original is untouched.")
            return True
        s = self.cad_edit
        if action == "__cad_undo":
            if s and s.undo():
                self._schedule_rebuild()
                await tell("Undone.")
            elif s:
                await tell("Nothing to undo.")
            else:
                await self.dispatcher.run("press_key", {"combo": "cmd+z"}, source=source)
            return True
        if action == "__cad_save":
            if s is None:
                await self.dispatcher.run("press_key", {"combo": "cmd+s"}, source=source)
                return True
            if not s.dirty:
                await tell("Nothing has changed.")
                return True
            try:
                backup = await asyncio.to_thread(s.save)
            except (OSError, RuntimeError) as exc:
                await tell(f"Not saved. {exc}"[:160])
                return True
            await self.bus.publish("cad_params", path=str(s.path), name=s.path.stem,
                                   params=s.state(), dirty=False)
            await self.bus.publish("log", level="info",
                                   text=f"saved {s.path.name}; original kept at {backup}")
            await tell(f"Saved to {s.path.name}. The original is backed up.")
            return True
        if s is None:
            await tell("Open an OpenSCAD model first. Its dimensions are what I can change.")
            return True
        if action == "__cad_reset":
            s.reset()
            self._schedule_rebuild()
            await tell("Back to the file as saved.")
            return True
        if action == "__cad_params":
            await self.bus.publish("cad_panel", open=True)
            names = [p.readable for p in s.params[:5]]
            more = f", and {len(s.params) - 5} more on the panel" if len(s.params) > 5 else ""
            await tell(f"{len(s.params)} dimensions. {', '.join(names)}{more}." if s.params
                       else "This file has no plain numbers to change.")
            return True
        if action == "__cad_done":
            await self.bus.publish("cad_select", name=None)
            await tell("Done.")
            return True
        if action == "__cad_ask":
            options = [s.by_name[n] for n in args.get("options", []) if n in s.by_name]
            if len(options) < 2:
                return False
            self._cad_pending = {"action": args["action"], "args": dict(args.get("args") or {}),
                                 "options": [o.name for o in options], "at": time.time()}
            await self.bus.publish("cad_ask", options=[o.name for o in options])
            said = ", ".join(o.readable for o in options[:-1]) + f", or {options[-1].readable}"
            await tell(f"Which one: {said}?" if len(options) > 2 else
                       f"{options[0].readable} or {options[1].readable}?")
            return True

        p = s.by_name.get(str(args.get("param", ""))) or s.find(str(args.get("param", "")))
        if p is None:
            _, options = s.resolve(str(args.get("param", "")))
            if len(options) >= 2 and speak:
                return await self.cad_action("__cad_ask", {"action": action, "args": {
                    k: v for k, v in args.items() if k != "param"}, "options": [o.name for o in options]},
                    source, speak)
            await tell(f"There is no {args.get('param', 'such')} in this part.")
            return True
        if action == "__cad_adjust":
            await self.bus.publish("cad_select", name=p.name)
            await tell(f"{p.readable}. Pinch and move your hand up or down.")
            return True
        try:
            if action == "__cad_set":
                value = s.set(p.name, args["value"])
            elif action == "__cad_bool":
                value = s.set(p.name, bool(args["on"]))
            elif action == "__cad_nudge":
                value = s.nudge(p.name, bool(args["up"]), args.get("amount"),
                                bool(args.get("percent")))
            else:
                return False
        except (TypeError, ValueError, KeyError):
            await tell("I could not read that number.")
            return True
        self._schedule_rebuild(p.name)
        await tell(f"{p.readable}, {self._say_value(p, value)}.")
        return True

    # ------------------------------------------------ geometry edits (Claude)

    async def _maybe_speak_up(self, status: Dict[str, Any]) -> None:
        """Say the one thing worth interrupting for. Usually nothing.

        The telemetry loop has always gathered every number this needs and
        published them to the HUD, where they sat as digits nobody was
        watching. The missing part was never the data.
        """
        # "Had" rather than "has": a thing that has never worked has not been
        # lost, and announcing its absence at startup is noise, not news.
        if status.get("vision_online"):
            self._had_vision = True
        if status.get("can_control"):
            self._had_control = True
        brain_ready = self.brain.status == "ready"
        if brain_ready:
            self._brain_was_ready = True

        unsaved_for = 0.0
        session = self.step_edit or self.cad_edit
        if session is not None and getattr(session, "dirty", False):
            self._dirty_since = self._dirty_since or time.time()
            unsaved_for = (time.time() - self._dirty_since) / 60.0
        else:
            self._dirty_since = 0.0

        state = dict(status)
        state.update(had_vision=self._had_vision, had_control=self._had_control,
                     brain_ready=brain_ready, brain_was_ready=self._brain_was_ready,
                     unsaved_for=unsaved_for,
                     # Never talk over speech, thinking, or a running edit.
                     busy=(self._modify_lock.locked() or not self.awake
                           or bool(self._cad_pending or self._modify_pending
                                   or self._smooth_pending)))
        line = self.watcher.tick(state)
        if not line:
            return
        self._activity("unprompted", said=line)
        self.memory.note("noticed", line)
        await self.say(line)

    async def look_at_model(self, views: int = 3, timeout: float = 8.0) -> List[str]:
        """Ask the HUD to photograph what is on screen. Returns PNG paths.

        The viewer already holds the geometry and a renderer; there is no
        second, headless renderer to keep in step with the first, and no
        question of the two disagreeing. What comes back is what the user is
        looking at.
        """
        import base64
        import pathlib
        import uuid

        if self.server is None or not self.server.clients:
            return []
        ident = uuid.uuid4().hex[:12]
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._captures[ident] = waiter
        await self.bus.publish("capture_request", id=ident, views=views, size=640)
        try:
            shots = await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            self._captures.pop(ident, None)
            return []

        out = config_module.ROOT / "logs" / "looks"
        out.mkdir(parents=True, exist_ok=True)
        # One set at a time: these are working files, not a gallery.
        for stale in out.glob("*.png"):
            stale.unlink(missing_ok=True)
        paths = []
        for i, shot in enumerate(shots):
            if not isinstance(shot, str) or "," not in shot:
                continue
            try:
                raw = base64.b64decode(shot.split(",", 1)[1])
            except (ValueError, TypeError):
                continue
            path = out / f"view{i}.png"
            path.write_bytes(raw)
            paths.append(str(path))
        return paths

    async def _verify_edit(self, request: str, said: str) -> Optional[str]:
        """Look at the result and say what is wrong with it, or None.

        This is the difference between a tool that guesses and one that
        checks. The numeric check catches an assembly that changed size by a
        factor; it cannot see a part left floating, a hole in the wrong face,
        or a nozzle turned inside out. Those are obvious in a picture and
        invisible in a bounding box.
        """
        if not self.cfg["ai"].get("verify_edits", True):
            return None
        # Let the viewer finish rebuilding and settle before the shot.
        await asyncio.sleep(float(self.cfg["ai"].get("verify_settle", 1.6)))
        shots = await self.look_at_model()
        if not shots:
            return None
        out = await self._claude_edit({"mode": "verify", "request": request,
                                       "said": said, "images": shots})
        if not out.get("wrong"):
            return None
        problem = str(out.get("say") or "").strip()
        self._activity("verify", request=request, problem=problem)
        return problem or None

    async def _claude_edit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        import pathlib
        import sys as _sys
        bridge = pathlib.Path(__file__).resolve().parents[1] / "bridge" / "claude_edit.py"

        # ai.edit_model overrides the bridge's default, so the model doing the
        # geometry can be changed in jarvis.json rather than only by env.
        env = dict(os.environ)
        chosen = str(self.cfg["ai"].get("edit_model") or "").strip()
        if chosen:
            env["JARVIS_CLAUDE_EDIT_MODEL"] = chosen
        timeout = float(self.cfg["ai"].get("edit_timeout") or 300)
        env["JARVIS_CLAUDE_EDIT_TIMEOUT"] = str(timeout)

        def call() -> Dict[str, Any]:
            proc = subprocess.run([_sys.executable, str(bridge)], input=json.dumps(payload),
                                  capture_output=True, text=True,
                                  timeout=timeout + 60, env=env)
            lines = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
            return json.loads(lines[-1]) if lines else {"say": "The edit came back empty.", "failed": True}
        return await asyncio.to_thread(call)

    async def modify_geometry(self, request: str, speak: bool = True) -> str:
        """A change dimensions cannot express ("add a hole", "make the nozzle
        longer" on a STEP): Claude writes the edit, JARVIS checks it builds,
        repairs once if not, and shows it. Nothing is written to disk."""
        request = request.strip()
        if not request:
            return "No change was asked for."
        if self._modify_lock.locked():
            await self.say("Still working on the last change.")
            return "busy"
        async with self._modify_lock:
            await self.bus.publish("cad_building", name="edit")
            await self.say("Working on it.")
            self._activity("modify_start", request=request,
                           file_kind="scad" if self.cad_edit else "step" if self.step_edit else None)
            if self.cad_edit is not None:
                line = await self._modify_scad(request)
            elif self.step_edit is not None:
                line = await self._modify_step(request)
            else:
                line = self._fixed_geometry_line() if self._shown is not None else \
                    "Open a model first."
            await self.bus.publish("cad_done")
            self._activity("modify_end", request=request, said=line)
            # The episode is what was asked for, not what was said back: the
            # request is the user's own words and is what they will use to
            # find it again.
            if self._shown is not None:
                self.memory.note("edit", f"{request} on {self._shown.stem.replace('_', ' ')}",
                                 file=self._shown.name, said=line)
                self.memory.save()
            await self.say(line)
            return line

    async def _modify_scad(self, request: str) -> str:
        s = self.cad_edit
        payload = {"mode": "scad", "request": request, "file": s.path.name, "source": s.source()}
        error = previous = None
        for attempt in range(2):
            if error:
                payload.update(error=error, previous=previous)
            out = await self._claude_edit(payload)
            self._activity("scad_edit", request=request, reply=out)
            if out.get("question"):
                self._modify_pending = {"request": request, "at": time.time()}
                return str(out.get("say") or "Which part do you mean?")
            if out.get("failed"):
                return str(out.get("say") or "The edit failed.")
            edits = out.get("edits") or []
            text = s.source()
            problem = None
            for e in edits:
                find, repl = str(e.get("find", "")), str(e.get("replace", ""))
                n = text.count(find) if find else 0
                if n != 1:
                    problem = f"'find' text occurs {n} times, must be exactly once: {find[:200]!r}"
                    break
                text = text.replace(find, repl, 1)
            if problem is None and not edits:
                problem = "no edits returned"
            if problem is None:
                s.set_source(text)
                built = await asyncio.to_thread(s.compile)
                if built is not None:
                    key = built.stem.rsplit("-", 1)[1]
                    await self.bus.publish("model_update", url=f"/api/model_edit?key={key}",
                                           params=s.state(), dirty=s.dirty, changed="source")
                    await self.bus.publish("cad_params", path=str(s.path), name=s.path.stem,
                                           params=s.state(), dirty=True)
                    return str(out.get("say") or "Done.")
                problem = f"OpenSCAD: {s.last_error}"
                s.undo()
            error, previous = problem, json.dumps(edits)[:4000]
        return f"I could not make that change work. {error[:120] if error else ''}"

    async def _modify_step(self, request: str) -> str:
        st = self.step_edit
        try:
            parts = await asyncio.to_thread(st.summary)
        except Exception as exc:
            return f"I could not read the parts of this file. {exc}"[:160]
        payload = {"mode": "step", "request": request, "file": st.path.name, "parts": parts,
                   "applied": [e["script"] for e in st.scripts]}
        error = previous = None
        for attempt in range(2):
            if error:
                payload.update(error=error, previous=previous)
            out = await self._claude_edit(payload)
            if out.get("question"):
                self._modify_pending = {"request": request, "at": time.time()}
                return str(out.get("say") or "Which part do you mean?")
            if out.get("failed"):
                return str(out.get("say") or "The edit failed.")
            script = str(out.get("script") or "").strip()
            if not script:
                error, previous = "no script returned", ""
                continue
            res = await asyncio.to_thread(st.try_script, script)
            self._activity("step_script", request=request, script=script, ok=res.get("ok"),
                           error=res.get("error"), touched=res.get("touched"))
            problem = self._size_check(res.get("touched") or []) if res.get("ok") else None
            if problem:
                self._activity("size_check", problem=problem)
                error, previous = problem, script
                continue
            if res.get("ok"):
                touched = res.get("touched") or []
                what = self._touched_line(touched)
                st.add(script, (str(out.get("say") or "") + (f" [{what}]" if what else "")).strip(), request)
                key = st.key()
                await self.bus.publish("model_update", url=f"/api/model_edit?key={key}", dirty=True)
                await self._publish_step_state()

                # Now look at it. The numeric check catches an assembly that
                # changed size by a factor; it cannot see a part left floating,
                # a bell turned inside out, or geometry burst into spikes.
                # Those are obvious in a picture and invisible in a bounding
                # box — which is exactly how the starburst got through.
                seen = await self._verify_edit(request, str(out.get("say") or ""))
                if seen and attempt == 0:
                    await self.bus.publish("log", level="warn",
                                           text=f"that looked wrong: {seen}")
                    st.undo()
                    await self._publish_step_state()
                    error, previous = (f"The edit applied, but the result looks wrong: {seen} "
                                       f"Rewrite the script so this does not happen."), script
                    continue
                # Spoken: Claude's sentence, which names the parts. The exact
                # list of changed parts goes to the panel and the activity log.
                said = str(out.get("say") or "Done.")
                return re.split(r"(?<=[.!?])\s", said.strip(), maxsplit=1)[0]
            error, previous = str(res.get("error", "failed")), script
        return f"I could not make that change work. {str(error)[:140]}"

    @staticmethod
    def _size_check(touched) -> Optional[str]:
        """A replacement that came out far larger than what it replaced is
        almost always a wrong axis or wrong units (a nozzle bell revolved flat
        came out 4 m wide). Returns the problem in numbers, or None."""
        boxes = next((n for v, n in touched if v == "_boxes"), None)
        if not boxes:
            return None

        if boxes.get("removed") and boxes.get("added"):
            r, a = boxes["removed"], boxes["added"]
            issues = []
            for k, ax in enumerate("xyz"):
                rs, as_ = r[k + 3] - r[k], a[k + 3] - a[k]
                if as_ > rs * 1.25 + 20:
                    issues.append(f"{ax}: new part spans {as_:.0f} mm where the removed parts spanned {rs:.0f} mm")
            if issues:
                return ("The replacement is much bigger than what it replaced (" + "; ".join(issues) +
                        f"). Removed parts bbox {[round(v) for v in r]}, new part bbox {[round(v) for v in a]}. "
                        "Check the axis and the radii/heights and fix the script.")

        # A stretch, scale or move removes nothing and adds nothing, so the
        # check above never saw one — and an assembly torn apart by a pair of
        # compounding 30% stretches passed as cleanly as a no-op. The whole
        # assembly is measured instead. The threshold is deliberately loose:
        # this is for the edit that has gone wrong by a factor, not for the
        # one that is 10% off.
        before, after = boxes.get("before"), boxes.get("after")
        if not before or not after:
            return None
        grew = []
        for k, ax in enumerate("xyz"):
            bs, as_ = before[k + 3] - before[k], after[k + 3] - after[k]
            if bs > 1.0 and as_ > bs * 1.6 + 20:
                grew.append(f"{ax}: {bs:.0f} mm to {as_:.0f} mm ({as_ / bs:.1f} times)")
        if not grew:
            return None
        return ("That script changed the size of the whole assembly far more than the request "
                "asked for (" + "; ".join(grew) + "). A change to one feature should not resize "
                "the assembly. Check that the selection catches only the parts meant, that the "
                "anchor is right, and that you are not scaling parts a previous edit already "
                "scaled. Fix the script.")

    @staticmethod
    def _touched_line(touched) -> str:
        """"Removed: eng rn inlet manifold exit." from the tool's record of which
        named parts an edit changed, grouped (60 tubes are one line)."""
        parts = []
        for verb, names in touched or []:
            if verb.startswith("_"):
                continue
            groups = []
            for n in names:
                g = re.sub(r"[_\-\s]*\d+$", "", n)
                if g not in groups:
                    groups.append(g)
            if groups:
                label = ", ".join(x.replace("_", " ") for x in groups[:3])
                more = f" and {len(groups) - 3} more" if len(groups) > 3 else ""
                count = f" ({len(names)} parts)" if len(names) > len(groups[:3]) else ""
                parts.append(f"{verb.capitalize()}: {label}{more}{count}")
        return "; ".join(parts)

    _EDIT_WORDS = re.compile(
        r"\b(?:make|set|change|increase|decrease|reduce|extend|lengthen|shorten|widen|narrow|"
        r"thicken|thin|resize|scale|move|reshape|modify|edit|round|fillet|chamfer|bigger|smaller|"
        r"longer|shorter|wider|thicker|thinner|remove|delete|take off|get rid of|replace|instead|add|"
        r"drill|hole|cut|fill|merge|split|turn into|convert|swap|flip|rotate|shift|extend)\b", re.I)

    def _fixed_geometry_request(self, text: str) -> bool:
        return (self.cad_edit is None and self._shown is not None and
                self._shown.suffix.lower() in (".step", ".stp", ".stl", ".obj") and
                bool(self._EDIT_WORDS.search(text)))

    def _fixed_geometry_line(self) -> str:
        kind = "a STEP export" if self._shown.suffix.lower() in (".step", ".stp") else "a mesh"
        return (f"{self._shown.stem.replace('_', ' ')} is {kind}, so its shape is fixed here. "
                "I can turn it, zoom it, or make it smooth. To reshape it, change it in the CAD "
                "program and export again.")

    async def smooth_model(self, speak: bool = True) -> str:
        """Smooth shading for whatever is on screen; a STEP is also converted
        again at finer detail (2.5 times the triangles, a few seconds)."""
        path = self._shown
        if path is None:
            line = "There is no model on screen."
            if speak:
                await self.say(line)
            return line
        name = path.stem.replace("_", " ")
        if path.suffix.lower() in (".step", ".stp"):
            if speak:
                await self.say(f"Smoothing the {name}. The finer mesh takes a few seconds.")
            out = await asyncio.to_thread(self.models.renderable, str(path), True)
            if out is None:
                line = f"The finer conversion failed. {getattr(self.models, 'last_error', '')}"[:160]
                await self.say(line)
                return line
            import urllib.parse
            url = "/api/model?detail=fine&path=" + urllib.parse.quote(str(path))
            await self.bus.publish("model_update", url=url, smooth=True)
            return f"{name} smoothed at finer detail"
        await self.bus.publish("model_update", url=None, smooth=True)
        line = f"{name}, smooth shading."
        if speak:
            await self.say(line)
        return line

    _ORDINALS = {"first": 0, "one": 0, "1": 0, "second": 1, "two": 1, "2": 1, "third": 2,
                 "three": 2, "3": 2, "fourth": 3, "four": 3, "4": 3}

    async def _answer_cad_question(self, text: str, source: str) -> bool:
        """Apply the question's edit to what the answer names: one option by
        name ("the height"), by position ("the second one"), several ("width
        and height"), or all of them ("both", "all of them"). Anything else
        drops the question and is handled as a new request."""
        pending, self._cad_pending = self._cad_pending, None
        s = self.cad_edit
        if s is None or time.time() - pending["at"] > 45:
            return False
        answer = intents._ADDRESSED.sub("", text.strip().strip(".,!?").lower()).strip()
        if re.match(r"^(?:no|never ?mind|cancel|forget it|neither|none)\b", answer):
            await self.say("Left as it is.")
            return True
        options = [s.by_name[n] for n in pending["options"] if n in s.by_name]
        chosen = []
        # Names first: "width and height", "both the width and the height".
        named = re.sub(r"\b(?:both|all|each|every|of|them|the|ones?|please|just)\b", " ", answer)
        for part in re.split(r"\s*(?:,|\band\b|\bplus\b|\bor\b)\s*", named):
            part = part.strip()
            if not part:
                continue
            p, _ = s.resolve(part, among=options)
            if p is None:
                ranked = s.candidates(part, among=options)
                p = ranked[0][0] if ranked and (len(ranked) == 1 or ranked[0][1] - ranked[1][1] > 0.05) else None
            if p is not None and p not in chosen:
                chosen.append(p)
        if not chosen:
            if re.search(r"\bboth\b", answer):
                if len(options) == 2:
                    chosen = options
                else:
                    self._cad_pending = dict(pending, at=time.time())
                    await self.say("Which two: " + ", ".join(o.readable for o in options[:-1]) +
                                   f", or {options[-1].readable}?")
                    return True
            elif re.search(r"\b(?:all|each|every|all of them|all three|all four)\b", answer):
                chosen = options
            elif re.search(r"\blast\b", answer):
                chosen = [options[-1]]
            elif len(answer.split()) <= 4:
                for word, i in self._ORDINALS.items():
                    if re.search(rf"\b{word}\b", answer) and i < len(options):
                        chosen = [options[i]]
                        break
        if not chosen:
            return False                       # not an answer: treat as a new request
        lines = []
        for p in chosen:
            self._cad_msg = ""
            await self.cad_action(pending["action"], dict(pending["args"], param=p.name), source, speak=False)
            if self._cad_msg:
                lines.append(self._cad_msg.rstrip("."))
        await self.say(("; ".join(lines) + ".") if lines else "Done.")
        return True

    # The model gets these while an editable part is on screen, with the part's
    # dimensions in its instructions, so "make the glasses fit a wider face" can
    # become the right edits, or a question if it cannot tell.
    _MEMORY_TOOLS = [
        {"name": "remember",
         "description": "Keep something across sessions: a correction, a preference, a fact about "
                        "a part or the project that you should not have to be told again. Use it "
                        "when the user says to remember something, and when they correct you. "
                        "Write it as one self-contained sentence.",
         "input_schema": {"type": "object", "properties": {"text": {"type": "string"}},
                          "required": ["text"]}},
        {"name": "forget",
         "description": "Drop remembered facts matching a phrase. Use when the user says to "
                        "forget something, or when something remembered turns out to be wrong.",
         "input_schema": {"type": "object", "properties": {"about": {"type": "string"}},
                          "required": ["about"]}},
    ]

    def _cad_tools(self):
        """Everything the model gets beyond the fixed registry: what is on
        screen, what is remembered, and what is known about the project."""
        tools, context = self._cad_tools_for_model()
        tools = list(tools) + list(self._MEMORY_TOOLS)
        blocks = [b for b in (context, self.memory.brief(), self.knowledge.brief()) if b]
        return tools, "\n\n".join(blocks)

    def _cad_tools_for_model(self):
        s = self.cad_edit
        smooth = {"name": "model_smooth",
                  "description": "Render the model on screen smoother: smooth shading, and for a STEP "
                                 "file a finer conversion. Use for smooth, less faceted, higher detail.",
                  "input_schema": {"type": "object", "properties": {}}}
        modify = {"name": "cad_modify",
                  "description": "Change the part's geometry in a way its dimensions cannot: add, "
                                 "remove, move, stretch, round or reshape features or parts. Pass the "
                                 "user's request made precise (which feature, how much). Takes 10-40 s.",
                  "input_schema": {"type": "object", "properties": {"request": {"type": "string"}},
                                   "required": ["request"]}}
        simple = lambda n, d: {"name": n, "description": d, "input_schema": {"type": "object", "properties": {}}}
        if self._shown is None:
            return [], ""
        if self.step_edit is not None:
            return [modify, smooth, simple("cad_undo", "Undo the last edit."),
                    simple("cad_reset", "Discard all edits."),
                    simple("cad_save", "Save the edited assembly as a new STEP file. Only when asked.")], (
                f"The user has the STEP assembly '{self._shown.stem}' open in the model viewer. It "
                "has no editable dimensions, but its parts can be moved, stretched, scaled, deleted, "
                "cut or added to: any change to its shape goes through cad_modify with a precise "
                "description. For smooth, less faceted or higher detail, call model_smooth.")
        if s is None or not s.params:
            return [smooth], (
                f"The user has '{self._shown.stem}' ({self._shown.name}) open in the model viewer. "
                "It is a mesh with no dimensions or named parts to edit here. If asked to "
                "reshape it, say so in one sentence and offer to make it smooth; for smooth, less "
                "faceted or higher detail, call model_smooth.")
        name = {"type": "string", "description": "exact dimension name from the list"}
        specs = [
            {"name": "cad_set_dimension", "description": "Set one dimension of the model on screen to a value.",
             "input_schema": {"type": "object", "properties": {"name": name, "value": {"type": "number"}},
                              "required": ["name", "value"]}},
            {"name": "cad_change_dimension",
             "description": "Make one dimension bigger or smaller: by an amount in the model's units, "
                            "by a percentage, or by 10% if no amount is given.",
             "input_schema": {"type": "object", "properties": {
                 "name": name, "direction": {"type": "string", "enum": ["bigger", "smaller"]},
                 "amount": {"type": "number"}, "percent": {"type": "boolean"}},
                 "required": ["name", "direction"]}},
            {"name": "cad_toggle", "description": "Switch a true/false setting of the model on screen.",
             "input_schema": {"type": "object", "properties": {"name": name, "on": {"type": "boolean"}},
                              "required": ["name", "on"]}},
            {"name": "cad_undo", "description": "Undo the last edit to the model.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "cad_reset", "description": "Discard all unsaved edits to the model.",
             "input_schema": {"type": "object", "properties": {}}},
            {"name": "cad_save", "description": "Write the edited dimensions into the model's file. Only when asked to save.",
             "input_schema": {"type": "object", "properties": {}}},
            smooth,
            modify,
        ]
        rows = []
        for p in s.params[:120]:
            cur = s.current(p.name)
            note = f"  ({p.comment})" if p.comment else ""
            rows.append(f"- {p.name} [{p.readable}] = {self._say_value(p, cur)}{note}")
        context = (
            f"The user has the part '{s.path.stem}' ({s.path.name}) open in the model viewer and can "
            "change it by voice. Its editable dimensions, in the file's units (usually mm):\n"
            + "\n".join(rows) +
            "\nRules for this part:\n"
            "1. Only change it when the user asks for a change. A question (how, why, what, should, "
            "could) gets a one-sentence answer that names the dimensions you would change and asks "
            "whether to do it; call no tool.\n"
            "2. Work out what the request means physically for this part and make every edit it "
            "implies, using the exact names above. \"Wider lenses\" is lens width alone; \"bigger "
            "lenses\" is width and height; fitting a wider face widens the spacing between the eyes "
            "and the parts that follow it, not just one gap.\n"
            "3. If you cannot tell which dimensions are meant, or how much, do not guess: ask one "
            "short question naming the two or three likely dimensions.\n"
            "4. After editing, say in one sentence what changed, with the new values. Do not ask "
            "whether they want anything else.")
        return specs, context

    async def _cad_tool_run(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self._activity("tool", name=name, args=args)
        if name == "remember":
            kept = self.memory.remember(str(args.get("text", "")), source="model")
            if not kept:
                return {"ok": False, "error": "nothing to remember"}
            await self.bus.publish("log", level="info", text=f"remembered: {kept}")
            return {"ok": True, "result": f"remembered: {kept}"}
        if name == "forget":
            gone = self.memory.forget(str(args.get("about", "")))
            return {"ok": True, "result": f"forgot {gone}" if gone else "nothing matched"}
        mapping = {
            "cad_set_dimension": ("__cad_set", {"param": args.get("name"), "value": args.get("value")}),
            "cad_change_dimension": ("__cad_nudge", {"param": args.get("name"),
                                                     "up": str(args.get("direction", "")).lower() in (
                                                         "bigger", "increase", "up", "larger", "longer", "more",
                                                         "raise", "wider", "thicker", "taller", "grow"),
                                                     "amount": args.get("amount"),
                                                     "percent": bool(args.get("percent"))}),
            "cad_toggle": ("__cad_bool", {"param": args.get("name"), "on": bool(args.get("on"))}),
            "cad_undo": ("__cad_undo", {}), "cad_reset": ("__cad_reset", {}), "cad_save": ("__cad_save", {}),
        }
        if name == "model_smooth":
            return {"ok": True, "result": await self.smooth_model(speak=False)}
        if name == "cad_modify":
            # Long: run it in the background and let the model say it has started.
            self._spawn(self.modify_geometry(str(args.get("request", ""))), "geometry edit")
            return {"ok": True, "result": "started; it takes 10-40 seconds and I will say when it is done"}
        if name not in mapping:
            return {"ok": False, "error": f"no such tool {name}"}
        action, a = mapping[name]
        if action in ("__cad_set", "__cad_nudge", "__cad_bool") and self.cad_edit and \
                str(a.get("param")) not in self.cad_edit.by_name:
            return {"ok": False, "error": f"no dimension named {a.get('param')}; use an exact name from the list"}
        self._cad_msg = ""
        await self.cad_action(action, a, "model", speak=False)
        return {"ok": True, "result": self._cad_msg or "done"}

    async def open_source(self, query: str, app_hint: str = "",
                          source: str = "voice") -> bool:
        """Open a CAD source file in the application that owns it.

        Returns False without speaking if there is no such file, so a caller
        that was only guessing — a failed app launch, say — can fall through to
        its own error instead of claiming a file it never found.
        """
        query = (query or "").strip()
        if not query:
            return False
        hit = self.models.best_source(query)
        if hit is None:
            return False
        app = {"openscad": "OpenSCAD", "fusion": "Autodesk Fusion 360",
               "solidworks": "SOLIDWORKS", "blender": "Blender",
               "onshape": "Google Chrome"}.get(app_hint, hit.get("app"))
        result = await self.dispatcher.run(
            "open_file", {"path": hit["path"], "app": app}, source=source)
        if not result.get("ok"):
            await self.say(f"That failed. {result.get('error', '')}"[:160])
            return True
        # Give the application a moment to come to the front, then render the
        # source and put the docks away — a .scad opens as text with an empty
        # viewport, so there is nothing to turn until it has been previewed.
        await asyncio.sleep(2.5)
        from .vision import cad as cad_module
        steps = await asyncio.get_running_loop().run_in_executor(
            None, cad_module.prepare_viewport, app or "")

        if self.gestures:
            if not self.gestures.armed:
                await self.gestures.set_armed(True, why="cad")
            await self.gestures.cad.enter(why="open", app=app or None)

        name = str(hit["name"]).replace("_", " ")
        failed = [s for s in steps if ":" in s]
        if steps and not failed:
            await self.say(f"{name}, previewed and framed. Pinch to orbit.")
        elif steps:
            await self.say(f"{name} is open, but {failed[0].split(':')[0]} did not "
                           f"take. Check Accessibility.")
        else:
            await self.say(f"{name} in {app}. I cannot drive its menus without "
                           f"Accessibility, so preview it yourself with F5.")
        return True

    @staticmethod
    def summarise(action: str, result: Any) -> str:
        """Turn a raw action result into one speakable sentence."""
        if action == "system_status" and isinstance(result, dict):
            parts = [f"CPU at {result.get('cpu', 0):.0f} percent",
                     f"memory at {result.get('mem_percent', 0):.0f} percent"]
            if "battery" in result:
                parts.append(f"battery {result['battery']} percent"
                             + (" and charging" if result.get("charging") else ""))
            if "disk_free_gb" in result:
                parts.append(f"{result['disk_free_gb']:.0f} gigabytes free")
            return "All systems nominal. " + ", ".join(parts) + "."
        if action == "list_apps" and isinstance(result, list):
            return f"{len(result)} applications running: " + ", ".join(result[:8]) + "."
        if action == "browser_tabs" and isinstance(result, list):
            if not result:
                return "No browser tabs open."
            titles = ", ".join(t["title"][:40] for t in result[:6])
            return f"{len(result)} tabs: {titles}."
        if action == "clipboard_read" and isinstance(result, str):
            return f"Clipboard holds: {result[:200]}" if result.strip() else "The clipboard is empty."
        if isinstance(result, str) and result:
            return result[0].upper() + result[1:] + "."
        return "Done."

    async def say(self, text: str) -> None:
        if not text:
            return
        await self.bus.publish("say", text=text)


def _port_holder(port: int) -> str:
    """Name the process sitting on the port, when we can find it."""
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=4).stdout
        lines = [l.split() for l in out.splitlines()[1:] if l.strip()]
        if lines:
            return f" by {lines[0][0]} (pid {lines[0][1]})"
    except Exception:
        pass
    return ""


def check_control() -> None:
    """Prove control, from inside the same process that needs it.

    This has to run through `jarvis-run`, not by calling python directly. The
    venv's python is a symlink to Apple's own signed com.apple.python3, and
    macOS makes an Apple-signed binary its own responsible process rather than
    inheriting the terminal's grant — so `python tests/test_control.py` asks a
    different question than the one that matters.
    """
    print("\n  control check\n")
    trusted = macos.accessibility_trusted()
    import os
    bundled = "JARVIS.app" in os.path.abspath(sys.argv[0]) or \
              os.environ.get("JARVIS_BUNDLED") == "1"
    # The app binary reports its own trust through the environment, because
    # that is the process whose signature TCC actually checks — this Python
    # child's own answer is always "no" and says nothing useful.
    app_trusted = os.environ.get("JARVIS_APP_TRUSTED") == "1"
    print(f"  JARVIS.app granted control: {'YES' if app_trusted else 'NO'}")
    print(f"  (this python child itself: {'granted' if trusted else 'not granted'} — "
          f"expected, and not what matters)")
    print(f"  Running as: {'JARVIS.app bundle' if bundled else 'a plain shell process'}")
    trusted = trusted or app_trusted
    if not trusted:
        if bundled:
            # Asking from inside the bundle makes macOS name J.A.R.V.I.S. in
            # the dialog and add it to the list itself, which is far more
            # reliable than asking someone to find the right file to drag.
            print("\n  Asking macOS for permission now — a dialog should appear.")
            macos.request_accessibility()
            macos.open_privacy_pane("Accessibility")
            print("  Allow it, or tick J.A.R.V.I.S. in the pane that just opened,")
            print("  then run ./check-control.sh again.\n")
        else:
            print("\n  Run ./check-control.sh instead — it launches the app")
            print("  bundle, which is the identity macOS can actually grant.\n")
        raise SystemExit(1)

    # The report can be optimistic; moving the cursor and reading it back is
    # the only proof that events reach the window server.
    start = macos.mouse_position()
    target = (start[0] + 60, start[1] + 40)
    macos.move_mouse(*target)
    time.sleep(0.25)
    landed = macos.mouse_position()
    macos.move_mouse(*start)
    moved = abs(landed[0] - target[0]) < 4 and abs(landed[1] - target[1]) < 4
    print(f"  Cursor moved to {int(target[0])},{int(target[1])}, read back "
          f"{int(landed[0])},{int(landed[1])}")
    print(f"  Synthetic mouse events: {'WORKING' if moved else 'IGNORED'}")
    if not moved:
        print("\n  The grant is stale. Remove the entry, add it again, restart.\n")
        raise SystemExit(1)

    macos.mouse_down("middle", ["shift"])
    macos.mouse_up("middle", ["shift"])
    print("  Middle-button + modifier drag: available (CAD orbit will work)")
    print("\n  Control is live. Gestures and CAD mode will move things.\n")


def check_permissions() -> None:
    """Name the exact apps that need each permission, and open each pane."""
    import os

    # macOS attaches a grant to the app it holds responsible for the process,
    # which is the terminal you launched from — not Python, and not J.A.R.V.I.S.
    term = os.environ.get("TERM_PROGRAM", "")
    app = {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm",
           "vscode": "Visual Studio Code", "WarpTerminal": "Warp",
           "ghostty": "Ghostty", "Hyper": "Hyper"}.get(term, term or "your terminal app")
    detached = os.getppid() <= 1

    print("\n  J.A.R.V.I.S. — permissions\n")
    if detached:
        print("  !  This process is detached, so macOS has no app to attach a")
        print("     grant to. Run this from your own Terminal window.\n")
    else:
        print(f"  You launched this from:  {app}\n")

    trusted = macos.accessibility_trusted()
    rows = [
        ("Accessibility", app, trusted,
         "move the mouse, press keys, manage windows"),
        ("Camera", app, None,
         "see your hands (macOS prompts the first time it opens)"),
        ("Microphone", "Google Chrome", None,
         "hear you — speech runs in the HUD page, not in Python"),
    ]
    print("  GIVE                TO                    FOR")
    for name, who, state, why in rows:
        mark = "" if state is None else ("  [granted]" if state else "  [MISSING]")
        print(f"  {name:<18}  {who:<20}  {why}{mark}")

    print("\n  Opening each pane now. Add the app with +, tick it, and for")
    print(f"  Accessibility restart {app} afterwards — the grant only applies")
    print("  to a freshly launched process.\n")

    if not trusted:
        macos.request_accessibility()
    for pane in ("Accessibility", "Camera", "Microphone"):
        macos.open_privacy_pane(pane)
        time.sleep(1.2)

    print("  The Microphone pane has NO + button: macOS lists an app only after")
    print("  that app has asked. To make Chrome ask, start J.A.R.V.I.S. and open")
    print(f"  http://127.0.0.1:{config_module.load()['server']['port']}/mic-test.html\n")
    print("  Check again with:  ./jarvis-run --permissions\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S.")
    parser.add_argument("--no-vision", action="store_true",
                        help="start without opening the camera")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not launch the HUD window")
    parser.add_argument("--port", type=int, help="override the HUD port")
    parser.add_argument("--write-config", action="store_true",
                        help="write jarvis.json with the defaults, then exit")
    parser.add_argument("--replace", action="store_true",
                        help="stop an already-running J.A.R.V.I.S. first")
    parser.add_argument("--check-control", action="store_true",
                        help="prove this process can actually move the mouse")
    parser.add_argument("--permissions", action="store_true",
                        help="check and request the macOS permissions, then exit")
    args = parser.parse_args()

    if args.write_config:
        print(f"wrote {config_module.write_default()}")
        return
    if args.permissions:
        check_permissions()
        return
    if args.check_control:
        check_control()
        return

    cfg = config_module.load()
    if args.port:
        cfg["server"]["port"] = args.port
    if args.replace:
        subprocess.run(["pkill", "-f", "jarvis.main"], check=False)
        time.sleep(2.0)
        print("  stopped the previous instance")
    try:
        asyncio.run(Jarvis(cfg).run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
