"""CAD navigation by hand.

Orbit, pan and zoom are the three things a 3D viewport needs constantly, and
they are exactly what a hand does better than a mouse: turning a model is a
wrist movement, not a click-and-drag.

Two decisions shape this module:

*Relative, not absolute.* The cursor is not warped to your hand. When a drag
starts, the current cursor position is the anchor and hand movement is added to
it. So you put the pointer in the viewport once, and after that the hand only
supplies deltas — which is what makes it usable in a real application instead of
a demo.

*Per-application profiles.* Every CAD package orbits with a different mouse
button: OpenSCAD with left, Blender with middle, Onshape with right, Fusion
with shift+middle. The frontmost application is looked up here, so the same
gesture does the right thing in whichever window you are actually in.
"""
from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Tuple

# button, modifiers held during the drag
Binding = Tuple[str, List[str]]


class _LowPass:
    def __init__(self) -> None:
        self.y: Optional[float] = None

    def __call__(self, x: float, alpha: float) -> float:
        self.y = x if self.y is None else alpha * x + (1.0 - alpha) * self.y
        return self.y


class OneEuro:
    """Smooths a noisy pointer without adding the lag that smoothing usually costs.

    MediaPipe's palm landmark wanders by one or two percent of frame width
    even when a hand is perfectly still. At the orbit gain that is twenty or
    thirty pixels of drag per frame from a hand that is not moving, which is
    what makes hand-driven rotation feel broken rather than merely imprecise.

    A fixed smoothing constant trades that shake for lag, and lag is worse:
    the model keeps turning after you stop. This filter varies its cutoff with
    hand speed instead — heavy smoothing while you hold still, almost none
    while you sweep — so a held pose is steady and a fast move still tracks.
    """

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.015,
                 d_cutoff: float = 1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x = _LowPass()
        self._dx = _LowPass()
        self._prev: Optional[float] = None
        self._t: Optional[float] = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x: float, now: float) -> float:
        if self._t is None:
            self._t, self._prev = now, x
            return self._x(x, 1.0)
        dt = max(now - self._t, 1e-3)
        self._t = now
        rate = (x - self._prev) / dt
        self._prev = x
        smooth_rate = self._dx(rate, self._alpha(self.d_cutoff, dt))
        cutoff = self.min_cutoff + self.beta * abs(smooth_rate)
        return self._x(x, self._alpha(cutoff, dt))


class _Pointer2D:
    """A filtered hand position plus the pointer acceleration curve.

    Acceleration is the other half of why a fixed mapping feels wrong. One
    gain cannot serve both "turn it round to see the back" and "nudge it five
    degrees": at the gain that makes the sweep comfortable, the nudge is
    impossible. So a slow hand gets a fraction of the gain and a fast one gets
    all of it, exactly as a trackpad does.
    """

    def __init__(self, min_cutoff: float, beta: float,
                 precision_speed: float, min_scale: float):
        self._fx = OneEuro(min_cutoff, beta)
        self._fy = OneEuro(min_cutoff, beta)
        self.precision_speed = max(precision_speed, 1e-3)
        self.min_scale = min_scale
        self._last: Optional[Tuple[float, float]] = None
        self._t: Optional[float] = None

    def delta(self, point: Tuple[float, float], now: float) -> Tuple[float, float]:
        """Movement since the last frame, filtered and gain-scaled."""
        smooth = (self._fx(point[0], now), self._fy(point[1], now))
        if self._last is None or self._t is None:
            self._last, self._t = smooth, now
            return 0.0, 0.0
        dx, dy = smooth[0] - self._last[0], smooth[1] - self._last[1]
        dt = max(now - self._t, 1e-3)
        self._last, self._t = smooth, now

        travel = math.hypot(dx, dy)
        # Below this the hand is holding a pose, not moving it. Filtered
        # residue at this scale is noise and must not reach the viewport.
        if travel < 1.5e-4:
            return 0.0, 0.0
        scale = self.min_scale + (1.0 - self.min_scale) * \
            min(1.0, (travel / dt) / self.precision_speed)
        return dx * scale, dy * scale


class Profile:
    def __init__(self, orbit: Binding, pan: Binding, invert_zoom: bool = False,
                 fit: Optional[str] = None,
                 prepare: Optional[List[Tuple[str, str, Optional[bool]]]] = None,
                 zoom_menu: Optional[Tuple[str, str, str]] = None):
        self.orbit = orbit
        self.pan = pan
        self.invert_zoom = invert_zoom
        self.fit = fit          # key combo for zoom-to-fit, where one exists
        # What to do to a freshly opened document before handing it to the
        # hands: (menu, item, want) — want None clicks, True/False drives a
        # checkable item to that state instead of blindly toggling it.
        self.prepare = prepare or []
        # (menu, zoom-in item, zoom-out item), for applications where scroll
        # is not reliable or Accessibility-driven scroll is not wanted.
        self.zoom_menu = zoom_menu

    def as_dict(self) -> dict:
        return {"orbit": list(self.orbit), "pan": list(self.pan),
                "invert_zoom": self.invert_zoom, "fit": self.fit,
                "prepare": [list(step) for step in self.prepare],
                "zoom_menu": list(self.zoom_menu) if self.zoom_menu else None}


# Keys are matched case-insensitively against the frontmost application name.
PROFILES: Dict[str, Profile] = {
    # A .scad file opens as text with an empty viewport — there is nothing to
    # turn until it is previewed. So: render it, put the two docks away so the
    # model has the window, then frame it. Editor and Console are checkable,
    # so they are driven to off rather than toggled, or a second open would
    # bring them back.
    "openscad":    Profile(("left", []),   ("right", []),          fit="cmd+shift+v",
                           prepare=[("Design", "Preview", None),
                                    ("Window", "Editor", False),
                                    ("Window", "Console", False),
                                    ("View", "View All", None)],
                           zoom_menu=("View", "Zoom In", "Zoom Out")),
    "blender":     Profile(("middle", []), ("middle", ["shift"]),  fit="home"),
    "fusion":      Profile(("middle", ["shift"]), ("middle", []),  fit="cmd+shift+e"),
    "solidworks":  Profile(("middle", []), ("middle", ["control"])),
    "rhinoceros":  Profile(("right", []),  ("right", ["shift"])),
    "freecad":     Profile(("middle", []), ("middle", ["shift"])),
    "cinema 4d":   Profile(("middle", ["option"]), ("middle", [])),
    "shapr3d":     Profile(("middle", []), ("middle", ["shift"])),
    # Browser CAD — Onshape, Tinkercad, three.js viewers — orbits with the
    # left button and pans with the right.
    "chrome":      Profile(("left", []),   ("right", [])),
    "safari":      Profile(("left", []),   ("right", [])),
    "firefox":     Profile(("left", []),   ("right", [])),
    # Middle-drag to orbit is the most common convention, so it is the fallback.
    "default":     Profile(("middle", []), ("middle", ["shift"])),
}


def prepare_viewport(app_name: str) -> List[str]:
    """Get a freshly opened document ready to be turned by hand.

    Returns one line per step describing what happened, including the ones
    that failed — a half-prepared viewport is worth saying out loud, because
    the difference between "it did not work" and "it rendered but the panels
    are still up" is the difference between a broken feature and a small one.

    Every step needs Accessibility. Without it the list comes back empty and
    the caller says so rather than pretending the window is ready.
    """
    from ..control import desktop

    _, profile = profile_for(app_name)
    if not profile.prepare:
        return []

    done: List[str] = []
    for menu, item, want in profile.prepare:
        # OpenSCAD stops answering AppleScript while it renders, and a click
        # sent during a preview comes back -1719 rather than queueing. Each
        # step therefore waits for the last one to finish thinking.
        if not desktop.wait_responsive(app_name, timeout=90.0):
            done.append(f"{item}: {app_name} stopped responding")
            break
        try:
            if want is None:
                desktop.click_menu(app_name, menu, item)
                done.append(item)
            else:
                done.append(desktop.set_menu_checked(app_name, menu, item, want))
        except desktop.ControlError as exc:
            done.append(f"{item}: {exc}")
    return done


def zoom_viewport(app_name: str, steps: int) -> int:
    """Zoom by menu item, for when scrolling the viewport is not reliable.

    Positive steps zoom in. Returns how many actually landed.
    """
    from ..control import desktop

    _, profile = profile_for(app_name)
    if not profile.zoom_menu or not steps:
        return 0
    menu, zoom_in, zoom_out = profile.zoom_menu
    item = zoom_in if steps > 0 else zoom_out
    landed = 0
    for _ in range(min(abs(steps), 12)):
        try:
            desktop.click_menu(app_name, menu, item)
            landed += 1
        except desktop.ControlError:
            break
    return landed


def profile_for(app_name: str) -> Tuple[str, Profile]:
    """Pick a profile by frontmost application name."""
    lowered = (app_name or "").lower()
    for key, profile in PROFILES.items():
        if key != "default" and key in lowered:
            return key, profile
    return "default", PROFILES["default"]


class CadMode:
    """Turns one hand into viewport navigation.

    pinch + move        orbit
    two fingers + move  pan
    open palm + move    zoom (vertical), for when only one hand is free
    two hands apart     zoom
    fist                leave CAD mode
    """

    def __init__(self, cfg: dict, bus, dispatcher):
        self.cfg = cfg
        self.bus = bus
        self.dispatcher = dispatcher
        self.active = False
        self.app = ""
        self.profile_name = "default"
        self.profile = PROFILES["default"]

        self._drag: Optional[str] = None       # "orbit" | "pan"
        self._anchor: Optional[Tuple[float, float]] = None   # cursor at drag start
        self._pointer: Optional[_Pointer2D] = None           # filter + acceleration
        self._offset: List[float] = [0.0, 0.0]               # travel since then
        self._zoom_ref: Optional[float] = None
        self._zoom_accum = 0.0                 # scroll too small to send yet
        self._zoom_filter: Optional[OneEuro] = None
        self._last_scroll = 0.0

    # ----------------------------------------------------------- lifecycle

    async def enter(self, why: str = "voice", app: Optional[str] = None) -> None:
        """Enter CAD mode, against `app` if the caller knows which one.

        Guessing from the frontmost window is right when you say "CAD mode"
        while looking at a viewport, and wrong the moment the HUD is the front
        window — which it is, whenever you have just watched J.A.R.V.I.S. open
        something. That bound the hands to Chrome's profile and aimed them at
        the interface instead of the model. So a caller that opened the file
        says which application it opened, and only the spoken command guesses.
        """
        from ..control import desktop
        try:
            self.app = app or desktop.frontmost_app()
            if app:
                desktop.activate_app(app)  # the drags have to land in its window
        except Exception:
            self.app = app or ""
        self.profile_name, self.profile = profile_for(self.app)
        self.active = True
        await self.bus.publish("cad_mode", active=True, app=self.app,
                               profile=self.profile_name, why=why,
                               bindings=self.profile.as_dict())

    async def leave(self, why: str = "voice") -> None:
        await self._release()
        self.active = False
        await self.bus.publish("cad_mode", active=False, why=why)

    def status(self) -> dict:
        return {"active": self.active, "app": self.app,
                "profile": self.profile_name,
                "bindings": self.profile.as_dict()}

    # --------------------------------------------------------------- frame

    async def on_hand(self, hand: dict, pose: str, now: float) -> None:
        if pose == "pinch":
            await self._move("orbit", hand, now)
        elif pose == "peace":
            await self._move("pan", hand, now)
        elif pose == "open_palm":
            await self._release()
            await self._zoom_by_hand(hand, now)
        else:
            await self._release()
            self._zoom_ref = None
            self._zoom_filter = None
            self._zoom_accum = 0.0
            if pose == "fist":
                await self.leave(why="fist")

    async def on_two_hands(self, left: dict, right: dict, now: float) -> None:
        """Both hands pinched: the distance between them is the zoom."""
        span = math.hypot(left["palm"][0] - right["palm"][0],
                          left["palm"][1] - right["palm"][1])
        if self._zoom_ref is None:
            self._zoom_ref = span
            return
        await self._zoom_delta(span - self._zoom_ref, now, ref=span)

    async def _zoom_delta(self, delta: float, now: float, ref: float) -> None:
        """Turn hand travel into scroll, accumulating what is too small to send.

        The old version dropped any movement under the threshold and then reset
        its reference anyway, so slow zooming registered as nothing at all
        while fast zooming arrived as a burst of clamped scrolls. Sub-threshold
        travel is now kept and added to the next frame, which is what makes a
        slow, deliberate zoom possible.
        """
        self._zoom_ref = ref
        self._zoom_accum += delta * float(self.cfg.get("zoom_gain", 260.0))
        if now - self._last_scroll < 0.03:
            return
        whole = int(self._zoom_accum)
        if whole == 0:
            return
        self._zoom_accum -= whole
        self._last_scroll = now
        await self._scroll(whole)

    # ---------------------------------------------------------- primitives

    def _new_pointer(self) -> "_Pointer2D":
        return _Pointer2D(
            min_cutoff=float(self.cfg.get("smoothing_cutoff", 1.0)),
            beta=float(self.cfg.get("smoothing_beta", 0.015)),
            precision_speed=float(self.cfg.get("precision_speed", 0.6)),
            min_scale=float(self.cfg.get("min_gain_scale", 0.25)),
        )

    async def _move(self, kind: str, hand: dict, now: Optional[float] = None) -> None:
        """Start or continue a drag, relative to where the cursor already is."""
        from ..control import desktop
        button, mods = getattr(self.profile, kind)
        point = tuple(hand["palm"])
        now = now if now is not None else time.time()

        if self._drag != kind:
            await self._release()
            self._drag = kind
            self._anchor = desktop.mouse_position()
            self._pointer = self._new_pointer()
            self._pointer.delta(point, now)          # seed, no movement yet
            self._offset = [0.0, 0.0]
            desktop.mouse_down(button, mods)
            await self.bus.publish("cad_drag", kind=kind, button=button,
                                   modifiers=mods, app=self.app)
            return

        # Accumulated, not mapped from the pose's starting point. An absolute
        # mapping cannot carry a variable gain — change the gain and the
        # cursor jumps to wherever the new scale puts the whole offset — and
        # it made every frame's jitter an absolute position error rather than
        # something that averages out.
        gain = float(self.cfg.get("orbit_gain" if kind == "orbit" else "pan_gain", 1600.0))
        dx, dy = self._pointer.delta(point, now)
        if dx == 0.0 and dy == 0.0:
            return
        self._offset[0] += dx * gain
        self._offset[1] += dy * gain
        desktop.drag_to(self._anchor[0] + self._offset[0],
                      self._anchor[1] + self._offset[1], button, mods)

    async def _release(self) -> None:
        if self._drag is None:
            return
        from ..control import desktop
        button, mods = getattr(self.profile, self._drag)
        kind, self._drag = self._drag, None
        self._anchor = self._pointer = None
        self._offset = [0.0, 0.0]
        desktop.mouse_up(button, mods)
        await self.bus.publish("cad_drag_end", kind=kind)

    async def _zoom_by_hand(self, hand: dict, now: float) -> None:
        """One-handed zoom: an open palm moved up or down."""
        if self._zoom_filter is None:
            self._zoom_filter = OneEuro(
                float(self.cfg.get("smoothing_cutoff", 1.0)),
                float(self.cfg.get("smoothing_beta", 0.015)))
        y = self._zoom_filter(hand["palm"][1], now)
        if self._zoom_ref is None:
            self._zoom_ref = y
            return
        await self._zoom_delta(self._zoom_ref - y, now, ref=y)

    async def _scroll(self, amount: float) -> None:
        from ..control import desktop
        if self.profile.invert_zoom:
            amount = -amount
        desktop.scroll(int(max(-60, min(60, amount))))

    async def fit(self) -> None:
        """Zoom to fit, where the application has a shortcut for it."""
        if not self.profile.fit:
            await self.bus.publish("log", level="warn",
                                   text=f"no fit shortcut known for {self.app}")
            return
        await self.dispatcher.run("press_key", {"combo": self.profile.fit},
                                  source="gesture")
