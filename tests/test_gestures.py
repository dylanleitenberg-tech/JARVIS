"""Gesture recognition tests driven by synthetic hands — no camera involved.

Builds MediaPipe-shaped 21-point hands for each pose, runs them through the
same `_describe_hand` geometry the tracker uses and then through the real
GestureEngine, and checks the right action fires.

    .venv/bin/python tests/test_gestures.py
"""
from __future__ import annotations

import asyncio
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jarvis import config as config_module
from jarvis.bus import Bus


async def _noop() -> None:
    """The bus awaits its handlers, so a recording handler must return one."""
from jarvis.vision.gestures import GestureEngine
from jarvis.vision.tracker import VisionWorker

WRIST = (0.50, 0.75)
MCP_Y = 0.60
FINGER_X = {"index": 0.44, "middle": 0.48, "ring": 0.52, "pinky": 0.56}


class _Point:
    """Stands in for a MediaPipe NormalizedLandmark."""

    def __init__(self, x, y, z=0.0):
        self.x, self.y, self.z = x, y, z


def build_hand(fingers=(True, True, True, True), thumb="out", pinch_to=None,
               offset=(0.0, 0.0)):
    """fingers: extension flags for index..pinky. thumb: out|tuck|up|down."""
    dx, dy = offset
    pts = [_Point(WRIST[0] + dx, WRIST[1] + dy)]

    thumb_chains = {
        "out":  [(0.44, 0.70), (0.40, 0.66), (0.36, 0.62), (0.33, 0.59)],
        "tuck": [(0.46, 0.70), (0.47, 0.66), (0.49, 0.64), (0.50, 0.63)],
        "up":   [(0.45, 0.69), (0.43, 0.64), (0.41, 0.59), (0.40, 0.55)],
        "down": [(0.45, 0.78), (0.43, 0.82), (0.41, 0.85), (0.40, 0.88)],
    }
    thumb_pts = list(thumb_chains[thumb])
    if pinch_to is not None:
        # Park the thumb tip on the named fingertip to make a pinch. The pinched
        # finger must be extended for the tip to be out where the thumb meets it.
        tx = FINGER_X[pinch_to] + 0.01
        thumb_pts[-1] = (tx, 0.45)
        thumb_pts[-2] = (tx - 0.03, 0.51)
        fingers = list(fingers)
        fingers[("index", "middle", "ring", "pinky").index(pinch_to)] = True
    for x, y in thumb_pts:
        pts.append(_Point(x + dx, y + dy))

    for i, name in enumerate(("index", "middle", "ring", "pinky")):
        x = FINGER_X[name]
        pts.append(_Point(x + dx, MCP_Y + dy))                   # MCP
        if fingers[i]:
            chain = [(x, 0.54), (x, 0.49), (x, 0.44)]            # PIP, DIP, TIP
        else:
            chain = [(x, 0.55), (x, 0.58), (x, 0.62)]            # curled
        for cx, cy in chain:
            pts.append(_Point(cx + dx, cy + dy))
    return pts


POSES = {
    "open_palm":   dict(fingers=(True, True, True, True), thumb="out"),
    "fist":        dict(fingers=(False, False, False, False), thumb="tuck"),
    "point":       dict(fingers=(True, False, False, False), thumb="tuck"),
    "peace":       dict(fingers=(True, True, False, False), thumb="tuck"),
    "thumbs_up":   dict(fingers=(False, False, False, False), thumb="up"),
    "thumbs_down": dict(fingers=(False, False, False, False), thumb="down"),
    "pinch":        dict(fingers=(True, False, False, False), thumb="tuck",
                         pinch_to="index"),
    "pinch_middle": dict(fingers=(False, True, False, False), thumb="tuck",
                         pinch_to="middle"),
    "pinch_ring":   dict(fingers=(False, False, True, False), thumb="tuck",
                         pinch_to="ring"),
    "pinch_pinky":  dict(fingers=(False, False, False, True), thumb="tuck",
                         pinch_to="pinky"),
}


class FakeDispatcher:
    def __init__(self):
        self.calls = []
        self.pending = None

    async def run(self, name, args=None, source="gesture", skip_confirm=False):
        self.calls.append((name, dict(args or {})))
        return {"ok": True, "action": name, "args": args or {}, "result": "ok"}

    async def confirm_pending(self, accept=True):
        self.calls.append(("__confirm", {"accept": accept}))
        return {"ok": True, "action": "__confirm", "result": "ok"}


def _describe(pts, label="right"):
    """_describe_hand touches no instance state, so call it unbound."""
    return VisionWorker._describe_hand(None, pts, label)


async def main() -> int:
    cfg = config_module.load()
    cfg["gestures"]["require_arm"] = True
    cfg["gestures"]["arm_hold"] = 0.0
    cfg["gestures"]["cooldown"] = 0.0

    failures = []

    # ---- geometry: does each synthetic pose classify as intended? ----------
    print("pose classification")
    from jarvis.vision.gestures import _classify
    for name, kwargs in POSES.items():
        hand = _describe(build_hand(**kwargs), "right")
        on = cfg["gestures"]["pinch_on"]
        pinching = [gap < on for gap in hand["pinches"]]
        got = _classify(hand, pinching)
        ok = got == name
        print(f"  {'PASS' if ok else 'FAIL'}  {name:12} -> {got:12} "
              f"(gaps={[f'{g:.2f}' for g in hand['pinches']]} "
              f"ext={hand['n_extended']})")
        if not ok:
            failures.append(f"classify {name} -> {got}")

    # ---- engine: does the bound action fire? ------------------------------
    print("\ngesture -> action")
    expectations = [
        ("pinch_middle", "right_click"),
        ("thumbs_up", "__confirm"),
        ("thumbs_down", "__confirm"),
    ]
    for pose, expect in expectations:
        bus = Bus()
        bus.bind_loop(asyncio.get_running_loop())
        dispatcher = FakeDispatcher()
        engine = GestureEngine(cfg, bus, dispatcher)
        engine.armed = True
        hand = _describe(build_hand(**POSES[pose]), "right")
        for _ in range(8):                       # hold the pose past STABLE_FRAMES
            await engine.on_vision({"hands": [hand]})
        fired = [c[0] for c in dispatcher.calls]
        ok = expect in fired
        print(f"  {'PASS' if ok else 'FAIL'}  {pose:12} -> {fired or '[]'}")
        if not ok:
            failures.append(f"{pose} fired {fired}")

    # ---- a fist disarms rather than closing a window ----------------------
    bus = Bus(); bus.bind_loop(asyncio.get_running_loop())
    dispatcher = FakeDispatcher()
    engine = GestureEngine(cfg, bus, dispatcher)
    engine.armed = True
    # A fist is a RESTING shape — a chin propped on it, a hand leaving frame —
    # so by default it must do nothing at all, however long it is held.
    fist = _describe(build_hand(**POSES["fist"]), "right")
    for _ in range(20):
        await engine.on_vision({"hands": [fist]})
        await asyncio.sleep(0.02)
    ok = engine.armed and not dispatcher.calls
    print(f"  {'PASS' if ok else 'FAIL'}  a long-held fist changes nothing "
          f"(armed={engine.armed}, calls={dispatcher.calls})")
    if not ok:
        failures.append("resting fist disarmed or fired")

    # And losing sight of the hand must not switch it off either.
    for _ in range(10):
        await engine.on_vision({"hands": []})
        await asyncio.sleep(0.05)
    ok = engine.armed
    print(f"  {'PASS' if ok else 'FAIL'}  hand leaving frame leaves it on "
          f"(armed={engine.armed})")
    if not ok:
        failures.append("hand leaving frame disarmed")

    # And a resting fist must not eat the cooldown. Seen live: a chin propped
    # on a fist logged "gesture: fist" three times a second, and each one took
    # the cooldown with it — so a real gesture made straight afterwards was
    # silently swallowed. An unbound gesture is not an event.
    bus = Bus(); bus.bind_loop(asyncio.get_running_loop())
    gesture_events = []
    bus.on("gesture", lambda event: gesture_events.append(event) or _noop())
    dispatcher = FakeDispatcher()
    engine = GestureEngine(config_module.load(), bus, dispatcher)
    engine.armed = True
    for _ in range(12):                       # rest on the fist
        await engine.on_vision({"hands": [fist]})
    while_resting = [e.get("name") for e in gesture_events]
    thumbs = _describe(build_hand(**POSES["thumbs_up"]), "right")
    for _ in range(8):                        # then a real, bound gesture
        await engine.on_vision({"hands": [thumbs]})
    ok = not while_resting and dispatcher.calls
    print(f"  {'PASS' if ok else 'FAIL'}  a resting fist is silent and does not "
          f"eat the cooldown (while resting={while_resting or '[]'}, "
          f"then fired={[c[0] for c in dispatcher.calls] or '[]'})")
    if not ok:
        failures.append("unbound fist published an event or blocked the next gesture")

    # ---- arming: a gesture must do nothing before the palm arms it --------
    print("\nsafety")
    bus = Bus(); bus.bind_loop(asyncio.get_running_loop())
    dispatcher = FakeDispatcher()
    cfg_armed = config_module.load()
    cfg_armed["gestures"]["cooldown"] = 0.0
    engine = GestureEngine(cfg_armed, bus, dispatcher)   # require_arm defaults true
    fist = _describe(build_hand(**POSES["fist"]), "right")
    for _ in range(10):
        await engine.on_vision({"hands": [fist]})
    ok = not dispatcher.calls and not engine.armed
    print(f"  {'PASS' if ok else 'FAIL'}  disarmed fist fires nothing ({dispatcher.calls})")
    if not ok:
        failures.append("disarmed fist fired")

    engine.cfg["arm_hold"] = 0.12
    palm = _describe(build_hand(**POSES["open_palm"]), "right")
    for _ in range(6):
        await engine.on_vision({"hands": [palm]})
        await asyncio.sleep(0.04)
    ok = engine.armed
    print(f"  {'PASS' if ok else 'FAIL'}  held open palm arms gesture control")
    if not ok:
        failures.append("palm did not arm")

    # ---- swipe: a fast lateral palm travel reads as swipe_left ------------
    print("\nswipe")
    bus = Bus(); bus.bind_loop(asyncio.get_running_loop())
    dispatcher = FakeDispatcher()
    engine = GestureEngine(cfg, bus, dispatcher)
    engine.armed = True
    for i in range(10):
        shifted = _describe(build_hand(**POSES["open_palm"],
                                       offset=(-0.05 * i, 0.0)), "right")
        await engine.on_vision({"hands": [shifted]})
        await asyncio.sleep(0.03)   # a swipe is defined by speed, so time must pass
    fired = [c for c in dispatcher.calls]
    ok = any(c[1].get("combo") == "cmd+tab" for c in fired)
    print(f"  {'PASS' if ok else 'FAIL'}  leftward palm travel -> {fired or '[]'}")
    if not ok:
        failures.append(f"swipe fired {fired}")

    # ---- model viewer up: an open hand belongs to the model -----------------
    # The same leftward travel that swiped above must not switch apps while the
    # HUD's viewer is open, and a palm held still must not open the radial menu.
    print("\nmodel viewer open")
    bus = Bus(); bus.bind_loop(asyncio.get_running_loop())
    dispatcher = FakeDispatcher()
    engine = GestureEngine(cfg, bus, dispatcher)
    engine.armed = True
    engine.viewer_open = True
    for i in range(10):
        shifted = _describe(build_hand(**POSES["open_palm"],
                                       offset=(-0.05 * i, 0.0)), "right")
        await engine.on_vision({"hands": [shifted]})
        await asyncio.sleep(0.03)
    fired = [c for c in dispatcher.calls]
    ok = not any(c[1].get("combo") == "cmd+tab" for c in fired)
    print(f"  {'PASS' if ok else 'FAIL'}  palm travel with the viewer up -> {fired or '[]'}")
    if not ok:
        failures.append(f"viewer-open swipe fired {fired}")
    palm = _describe(build_hand(**POSES["open_palm"]), "right")
    for _ in range(12):
        await engine.on_vision({"hands": [palm]})
        await asyncio.sleep(0.1)
    ok = engine.radial is None
    print(f"  {'PASS' if ok else 'FAIL'}  held palm with the viewer up opens no radial menu")
    if not ok:
        failures.append("viewer-open palm opened the radial menu")
    engine.viewer_open = False
    for _ in range(12):
        await engine.on_vision({"hands": [palm]})
        await asyncio.sleep(0.1)
    ok = engine.radial is not None
    print(f"  {'PASS' if ok else 'FAIL'}  same palm with the viewer closed opens it")
    if not ok:
        failures.append("palm did not open the radial menu once the viewer closed")

    # ---- whose hand: a second person's hand must not take over ----------
    print("\nhand ownership")
    from jarvis.vision.tracker import HandOwner
    def scaled(pts, k, cx, cy):
        return [_Point(cx + (p.x - cx) * k, cy + (p.y - cy) * k) for p in pts]
    mine = _describe(build_hand(**POSES["open_palm"]), "right")                       # at the keyboard
    theirs = _describe(scaled(build_hand(**POSES["open_palm"], offset=(-0.3, 0.0)), 1.5, 0.2, 0.55), "left")  # bigger, off to the side
    own = HandOwner()
    own.mark([mine], 10.0)
    a = mine["owner"]
    own.mark([mine, theirs], 10.1)
    b = (mine["owner"], theirs["owner"])
    own.mark([theirs], 10.5)          # mine stepped out half a second ago
    c = theirs["owner"]
    own.mark([theirs], 11.6)          # gone for over a second: the hand in view takes over
    d = theirs["owner"]
    ok = a and b == (True, False) and not c and d
    print(f"  {'PASS' if ok else 'FAIL'}  first hand kept as owner: {a}, {b}; stranger waits {not c}, then takes over {d}")
    if not ok:
        failures.append(f"ownership {a} {b} {c} {d}")
    # the same person's other hand is accepted alongside
    other = _describe(build_hand(**POSES["open_palm"], offset=(0.2, 0.0)), "left")
    own = HandOwner(); own.mark([mine, other], 20.0)
    ok = mine["owner"] and other["owner"]
    print(f"  {'PASS' if ok else 'FAIL'}  the user's own second hand is accepted: {mine['owner']}, {other['owner']}")
    if not ok:
        failures.append("second hand of the same person rejected")

    print("\n" + ("ALL PASS" if not failures else f"FAILURES:\n  " + "\n  ".join(failures)))
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
