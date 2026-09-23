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
        intents.model_lookup = self.models.best

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
                             models=self.models)
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
            except Exception as exc:
                await self.bus.publish("log", level="warn", text=f"telemetry: {exc}")
            await asyncio.sleep(3.0)

    # -------------------------------------------------------- HUD messages

    async def on_hud_message(self, msg: dict, ws) -> None:
        kind = msg.get("type")

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
        elif kind == "ready":
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

        intent = intents.match(text)
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
            if action == "__open_source":
                query = str(args.get("query", "")).strip()
                if not await self.open_source(query, str(args.get("app", "")), source):
                    await self.say(f"I have no source file called {query}.")
                return

            if action == "__model":
                query = str(args.get("query", "")).strip()
                hit = self.models.best(query) if query else None
                await self.bus.publish("show_model", model=hit, query=query)
                if hit:
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
            if action == "__confirm":
                result = await self.dispatcher.confirm_pending(bool(args["accept"]))
                if result.get("cancelled"):
                    await self.say("Cancelled.")
                elif result.get("ok"):
                    await self.say("Done.")
                elif result.get("error"):
                    await self.say("Nothing was waiting on you, Sir.")
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

        # Nothing matched locally — hand it to the model.
        reply = await self.brain.ask(text)
        await self.say(reply)

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
