"""Gesture recognition and binding.

Turns the per-frame hand descriptions from `tracker` into discrete gestures
(fist, swipe, thumbs-up) and continuous modes (cursor, drag, two-hand zoom),
then fires the bound action.

Three things keep this from misfiring on ordinary hand movement:
  * arming — gestures do nothing until you hold an open palm, or say so;
  * stability — a static pose must hold for several consecutive frames;
  * cooldown — one discrete gesture per `gestures.cooldown` seconds.
"""
from __future__ import annotations

import math
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

# The region of the camera frame mapped onto the whole screen. Reaching the
# literal edge of frame is uncomfortable, so the usable box is inset.
ACTIVE_BOX = (0.16, 0.12, 0.84, 0.80)  # x0, y0, x1, y1

STABLE_FRAMES = 4          # frames a static pose must persist to count
SWIPE_WINDOW = 0.35        # seconds of travel history used for swipe detection
SWIPE_MIN_TRAVEL = 0.22    # normalised distance a swipe must cover

# A pinch that is released quickly and without travel is a click; anything
# longer or further is a drag. Both are one mouse-down/up pair, so the OS sees
# exactly one gesture either way.
CLICK_MAX_HOLD = 0.6       # seconds
CLICK_MAX_TRAVEL = 0.02    # normalised screen distance

RADIAL_ITEMS = [
    ("mission_control", "SPACES"),
    ("screenshot", "CAPTURE"),
    ("new_tab", "NEW TAB"),
    ("close_window", "CLOSE"),
    ("play_pause", "MEDIA"),
    ("show_desktop", "DESKTOP"),
    ("app_switcher", "SWITCH"),
    ("system_status", "STATUS"),
]


MIN_PINCH_REACH = 1.3   # fingertip must be this many hand-scales from the wrist

FINGERS = ("index", "middle", "ring", "pinky")
# The thumb-to-index pinch keeps the plain name, since it is the one people
# reach for first and the one bound to click.
PINCH_NAMES = ("pinch", "pinch_middle", "pinch_ring", "pinch_pinky")


def _classify(hand: dict, pinching) -> str:
    """Name the static pose of one hand.

    `pinching` is a 4-tuple of booleans, one per finger, already through
    hysteresis. The first finger found pinching wins, so index takes priority
    when a sloppy pinch closes two gaps at once.
    """
    ext: List[bool] = hand["extended"]
    n = hand["n_extended"]
    if isinstance(pinching, bool):           # tolerate the old single-pinch call
        pinching = (pinching, False, False, False)
    reaches = hand.get("reaches") or [hand.get("index_reach", 0.0), 0.0, 0.0, 0.0]
    for i, is_pinching in enumerate(pinching):
        # A fist also closes the thumb-finger gap, so a pinch additionally
        # requires that fingertip to be held away from the palm.
        if is_pinching and reaches[i] >= MIN_PINCH_REACH:
            return PINCH_NAMES[i]
    if n == 0:
        if hand.get("thumb_up"):
            return "thumbs_up"
        if hand.get("thumb_down"):
            return "thumbs_down"
        return "fist"
    if n == 4:
        return "open_palm"
    if ext[0] and not any(ext[1:]):
        return "point"
    if ext[0] and ext[1] and not ext[2] and not ext[3]:
        return "peace"
    return "unknown"


class HandState:
    """Per-hand history: pinch hysteresis, pose stability, motion trail."""

    def __init__(self) -> None:
        self.pinching = [False, False, False, False]   # index, middle, ring, pinky
        self.pose = "none"
        self.pose_since = 0.0
        self.stable_count = 0
        self.trail: Deque[Tuple[float, float, float]] = deque(maxlen=24)
        self.scroll_y: Optional[float] = None
        self.last_seen = 0.0

    def update(self, hand: dict, now: float, pinch_on: float, pinch_off: float) -> str:
        gaps = hand.get("pinches") or [hand["pinch"], 9.0, 9.0, 9.0]
        # Hysteresis per finger: it takes a tighter pinch to start than to keep.
        for i, gap in enumerate(gaps[:4]):
            self.pinching[i] = gap < (pinch_off if self.pinching[i] else pinch_on)

        pose = _classify(hand, self.pinching)
        if pose == self.pose:
            self.stable_count += 1
        else:
            self.pose, self.stable_count, self.pose_since = pose, 1, now
        self.last_seen = now
        self.trail.append((now, hand["palm"][0], hand["palm"][1]))
        return pose

    def held_for(self, now: float) -> float:
        return now - self.pose_since

    def swipe(self, now: float, min_velocity: float) -> Optional[str]:
        """Direction of a fast, straight travel inside the recent window."""
        recent = [p for p in self.trail if now - p[0] <= SWIPE_WINDOW]
        if len(recent) < 4:
            return None
        t0, x0, y0 = recent[0]
        t1, x1, y1 = recent[-1]
        dt = t1 - t0
        if dt < 0.05:
            return None
        dx, dy = x1 - x0, y1 - y0
        travel = math.hypot(dx, dy)
        if travel < SWIPE_MIN_TRAVEL or travel / dt < min_velocity:
            return None
        if abs(dx) > abs(dy) * 1.5:
            return "swipe_right" if dx > 0 else "swipe_left"
        if abs(dy) > abs(dx) * 1.5:
            return "swipe_down" if dy > 0 else "swipe_up"
        return None


class GestureEngine:
    """Consumes vision frames, emits gestures, runs the bound actions."""

    def __init__(self, config: dict, bus, dispatcher):
        self.cfg = config["gestures"]
        self.bus = bus
        self.dispatcher = dispatcher
        self.enabled = bool(self.cfg.get("enabled", True))
        self.armed = not self.cfg.get("require_arm", True)
        # Turned on deliberately (button, voice) it stays on. Only a palm-hold
        # arm times out, because that one can happen by accident.
        self.sticky = False
        self.hands: Dict[str, HandState] = {}
        self.last_fire = 0.0
        self.last_activity = time.time()
        self.cursor: Optional[Tuple[float, float]] = None
        self.dragging = False
        self.zoom_base: Optional[float] = None
        self.radial: Optional[Dict[str, Any]] = None
        self.last_gesture = "none"
        # The HUD's model viewer takes the open hand for itself while it is up.
        self.viewer_open = False
        from .cad import CadMode
        self.cad = CadMode(config.get("cad", {}), bus, dispatcher)

    # ------------------------------------------------------------ arming

    async def set_armed(self, armed: bool, why: str = "voice") -> None:
        if armed == self.armed:
            return
        self.armed = armed
        self.sticky = armed and why in ("hud", "voice", "cad", "button")
        self.last_activity = time.time()
        if not armed:
            await self._release_drag()
            if self.cad.active:
                await self.cad.leave(why="disarm")
            self.radial = None
        await self.bus.publish("gesture_armed", armed=armed, why=why)

    # ------------------------------------------------------------- frame

    async def on_vision(self, event: dict) -> None:
        if not self.enabled:
            return
        now = time.time()
        # Reject anything too small in frame to be a hand deliberately raised to
        # the camera. Hand tracking will happily fit 21 landmarks to background
        # clutter, and a spurious far-away "hand" must never arm or click.
        min_scale = float(self.cfg.get("min_hand_scale", 0.07))
        hands = [h for h in (event.get("hands") or [])
                 if h.get("scale", 0.0) >= min_scale and h.get("owner", True)]
        by_label: Dict[str, dict] = {}
        for hand in hands:
            label = hand["label"]
            if label in by_label:          # two of the same chirality: keep the nearer
                if hand["scale"] <= by_label[label]["scale"]:
                    continue
            by_label[label] = hand

        for label in list(self.hands):
            if label not in by_label and now - self.hands[label].last_seen > 0.6:
                # Forget the hand and let go of any button it was holding, but
                # never change the armed state: a hand leaving the frame is not
                # an instruction, and treating it as one made gesture control
                # switch itself off every time you lowered your arm.
                del self.hands[label]
                if self.dragging:
                    await self._release_drag()

        poses = {}
        for label, hand in by_label.items():
            state = self.hands.setdefault(label, HandState())
            poses[label] = state.update(hand, now, self.cfg["pinch_on"], self.cfg["pinch_off"])

        if not self.armed:
            await self._check_arm(by_label, poses, now)
            return

        if not self.sticky and self.cfg.get("disarm_after_idle") and \
                now - self.last_activity > float(self.cfg["disarm_after_idle"]) \
                and not by_label:
            await self.set_armed(False, why="idle")
            return
        if by_label:
            self.last_activity = now

        if self.cad.active:
            if len(by_label) == 2 and set(poses.values()) == {"pinch"}:
                await self.cad.on_two_hands(by_label["left"], by_label["right"], now)
            else:
                self.cad._zoom_ref = None
                for label, hand in by_label.items():
                    await self.cad.on_hand(hand, poses[label], now)
                    break          # one hand drives the viewport
            return

        if self.radial is not None:
            await self._radial_frame(by_label, poses, now)
            return

        if len(by_label) == 2:
            await self._two_hand(by_label, poses, now)
            return
        self.zoom_base = None

        for label, hand in by_label.items():
            await self._one_hand(label, hand, poses[label], now)

    async def _check_arm(self, by_label, poses, now: float) -> None:
        """An open palm held still for `arm_hold` seconds turns gestures on."""
        for label, pose in poses.items():
            state = self.hands[label]
            if pose == "open_palm" and state.held_for(now) >= float(self.cfg["arm_hold"]):
                await self.set_armed(True, why="palm")
                self.last_fire = now  # don't let the arming palm also fire a gesture
                return

    # ---------------------------------------------------------- one hand

    async def _one_hand(self, label: str, hand: dict, pose: str, now: float) -> None:
        state = self.hands[label]
        bindings = self.cfg["bindings"]

        # Continuous: index finger drives the cursor.
        if pose in ("point", "pinch") and bindings.get("point") == "cursor":
            await self._move_cursor(hand)
            # The button goes down when a pinch is established and up when it is
            # released. A short, still press is a click; a long or travelling one
            # is a drag. Nothing fires on a single stray frame of pinch.
            if pose == "pinch" and not self.dragging \
                    and state.stable_count >= STABLE_FRAMES:
                from ..control import macos
                self.dragging = True
                self.drag_started = now
                self.drag_origin = self.cursor
                macos.mouse_down()
                await self.bus.publish("gesture", name="press", hand=label)
            elif pose == "point" and self.dragging:
                await self._release_drag(now)
            return

        if self.dragging:
            await self._release_drag(now)

        if pose == "peace" and bindings.get("peace") == "scroll":
            if state.stable_count >= 2:
                await self._scroll(hand, state, now)
            return
        state.scroll_y = None

        if state.stable_count < STABLE_FRAMES:
            return

        # While the model viewer is up, an open hand is turning the model: a
        # fast sweep must not switch apps and a held palm must not open a menu.
        if pose == "open_palm" and self.viewer_open:
            state.trail.clear()
            return

        swipe = state.swipe(now, float(self.cfg["swipe_velocity"]))
        if pose == "open_palm" and swipe:
            state.trail.clear()
            await self._fire(swipe, label)
            return

        if pose == "open_palm" and state.held_for(now) >= 0.9 \
                and bindings.get("open_palm_hold") == "radial_menu":
            await self._open_radial(hand, label)
            return

        # Thumb-to-middle, -ring and -pinky are discrete buttons; only the
        # thumb-index pinch drives the cursor.
        # Turning gesture control OFF must be deliberate. A hand that is
        # half out of frame, or mid-transition between poses, reads as a fist
        # within a couple of frames — which switched it off a second after it
        # was switched on. So this one gesture needs a real hold.
        if pose == "fist" and bindings.get("fist") == "disarm":
            if state.held_for(now) >= float(self.cfg.get("disarm_hold", 0.9)):
                await self._fire(pose, label)
            return

        if pose in ("fist", "thumbs_up", "thumbs_down", "peace",
                    "pinch_middle", "pinch_ring", "pinch_pinky"):
            await self._fire(pose, label)

    async def _scroll(self, hand: dict, state: "HandState", now: float) -> None:
        """Two fingers out, moved vertically, scrolls the view under the cursor.

        This is the one everyday action a pointing device must have, and it has
        no discrete equivalent — so it is a mode, driven by travel, not a pose
        that fires once.
        """
        y = hand["index_tip"][1]
        last = getattr(state, "scroll_y", None)
        state.scroll_y = y
        if last is None:
            return
        gain = float(self.cfg.get("scroll_gain", 900.0))
        dy = (last - y) * gain
        if abs(dy) < 1.0:
            return
        from ..control import macos
        macos.scroll(int(max(-120, min(120, dy))))
        if now - getattr(self, "_scroll_logged", 0.0) > 0.5:
            self._scroll_logged = now
            await self.bus.publish("gesture", name="scroll", hand=hand["label"],
                                   action="scroll")

    async def _move_cursor(self, hand: dict) -> None:
        x0, y0, x1, y1 = ACTIVE_BOX
        nx = (hand["index_tip"][0] - x0) / (x1 - x0)
        ny = (hand["index_tip"][1] - y0) / (y1 - y0)
        nx, ny = min(max(nx, 0.0), 1.0), min(max(ny, 0.0), 1.0)
        alpha = float(self.cfg["cursor_smoothing"])
        if self.cursor is None:
            self.cursor = (nx, ny)
        else:
            self.cursor = (self.cursor[0] + alpha * (nx - self.cursor[0]),
                           self.cursor[1] + alpha * (ny - self.cursor[1]))
        from ..control import macos
        if self.dragging:
            w, h = macos.screen_size()
            macos.drag_to(self.cursor[0] * w, self.cursor[1] * h)
        else:
            macos.move_mouse_norm(*self.cursor)
        await self.bus.publish("cursor", x=round(self.cursor[0], 4), y=round(self.cursor[1], 4),
                               dragging=self.dragging)

    async def _release_drag(self, now: Optional[float] = None) -> None:
        if not self.dragging:
            return
        now = now or time.time()
        self.dragging = False
        from ..control import macos
        macos.mouse_up()
        held = now - getattr(self, "drag_started", now)
        origin = getattr(self, "drag_origin", None) or self.cursor or (0.0, 0.0)
        travel = math.hypot((self.cursor or origin)[0] - origin[0],
                            (self.cursor or origin)[1] - origin[1])
        clicked = held <= CLICK_MAX_HOLD and travel <= CLICK_MAX_TRAVEL
        await self.bus.publish("gesture", name="click" if clicked else "drag_end",
                               action="click" if clicked else "drag",
                               held=round(held, 2), travel=round(travel, 3))

    # ---------------------------------------------------------- two hands

    async def _two_hand(self, by_label, poses, now: float) -> None:
        if set(poses.values()) != {"pinch"}:
            self.zoom_base = None
            return
        left, right = by_label.get("left"), by_label.get("right")
        if not left or not right:
            return
        span = math.hypot(left["palm"][0] - right["palm"][0],
                          left["palm"][1] - right["palm"][1])
        if self.zoom_base is None:
            self.zoom_base = span
            await self.bus.publish("gesture", name="zoom_start")
            return
        ratio = span / max(self.zoom_base, 1e-3)
        if ratio > 1.25 and now - self.last_fire > 0.28:
            self.zoom_base = span
            self.last_fire = now
            await self.dispatcher.run("press_key", {"combo": "cmd+="}, source="gesture")
            await self.bus.publish("gesture", name="zoom_in")
        elif ratio < 0.8 and now - self.last_fire > 0.28:
            self.zoom_base = span
            self.last_fire = now
            await self.dispatcher.run("press_key", {"combo": "cmd+-"}, source="gesture")
            await self.bus.publish("gesture", name="zoom_out")

    # -------------------------------------------------------- radial menu

    async def _open_radial(self, hand: dict, label: str) -> None:
        self.radial = {"center": list(hand["palm"]), "hand": label,
                       "opened": time.time(), "selection": None}
        await self.bus.publish("radial_open", center=self.radial["center"],
                               items=[{"action": a, "label": t} for a, t in RADIAL_ITEMS])

    async def _radial_frame(self, by_label, poses, now: float) -> None:
        radial = self.radial
        hand = by_label.get(radial["hand"]) or next(iter(by_label.values()), None)
        if hand is None:
            if now - radial["opened"] > 1.5:
                self.radial = None
                await self.bus.publish("radial_close", chosen=None)
            return

        cx, cy = radial["center"]
        dx, dy = hand["index_tip"][0] - cx, hand["index_tip"][1] - cy
        reach = math.hypot(dx, dy)
        selection = None
        if reach > 0.055:
            angle = (math.degrees(math.atan2(dy, dx)) + 360.0) % 360.0
            selection = int(((angle + 360.0 / len(RADIAL_ITEMS) / 2) % 360.0)
                            / (360.0 / len(RADIAL_ITEMS)))
        radial["selection"] = selection
        await self.bus.publish("radial_update", selection=selection, reach=round(reach, 3))

        pose = poses.get(radial["hand"])
        if pose == "pinch" and selection is not None:
            chosen = RADIAL_ITEMS[selection][0]
            self.radial = None
            self.last_fire = now
            await self.bus.publish("radial_close", chosen=chosen)
            await self.dispatcher.run(chosen, {}, source="gesture")
        elif pose == "fist" or now - radial["opened"] > 8.0:
            self.radial = None
            await self.bus.publish("radial_close", chosen=None)

    # ------------------------------------------------------------- firing

    async def _fire(self, gesture: str, hand: str) -> None:
        target = self.cfg["bindings"].get(gesture)
        if not target:
            # A gesture bound to nothing is not an event. Announcing it filled
            # the activity log with "gesture: fist" three times a second while
            # a hand simply rested — and worse, it took the cooldown with it,
            # so a real gesture made just after a resting fist was swallowed.
            # Still recorded, because "what does it think my hand is doing"
            # is a question the HUD has to be able to answer.
            self.last_gesture = gesture
            return

        now = time.time()
        if now - self.last_fire < float(self.cfg["cooldown"]):
            return
        self.last_fire = now
        self.last_gesture = gesture
        await self.bus.publish("gesture", name=gesture, hand=hand, action=target)

        if target in ("confirm", "dismiss"):
            result = await self.dispatcher.confirm_pending(accept=(target == "confirm"))
            await self.bus.publish("gesture_result", gesture=gesture, **result)
            return
        if target == "disarm":
            await self.set_armed(False, why="fist")
            return
        if target in ("cursor", "drag", "zoom", "radial_menu", "scroll"):
            return  # handled as continuous modes above

        mapped = {"next_app": ("press_key", {"combo": "cmd+tab"}),
                  "prev_app": ("press_key", {"combo": "cmd+shift+tab"})}
        if target in mapped:
            name, args = mapped[target]
        else:
            name, args = target, {}
        result = await self.dispatcher.run(name, args, source="gesture")
        await self.bus.publish("gesture_result", gesture=gesture, **result)
