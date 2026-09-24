"""CAD mode: does the right mouse button get dragged for each application?

No camera and no real mouse — macos calls are captured, so this checks the
mapping and the relative-drag maths rather than the synthesis.

    .venv/bin/python tests/test_cad.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jarvis import config as config_module
from jarvis.bus import Bus
from jarvis.control import desktop as macos    # whichever backend this OS uses
from jarvis.vision import cad


class Recorder:
    """Stands in for the mouse, and remembers what it was told."""

    def __init__(self):
        self.calls = []
        self.pos = (500.0, 400.0)

    def install(self):
        macos.mouse_position = lambda: self.pos
        macos.mouse_down = lambda b="left", m=None: self.calls.append(("down", b, tuple(m or ())))
        macos.mouse_up = lambda b="left", m=None: self.calls.append(("up", b, tuple(m or ())))
        macos.drag_to = lambda x, y, b="left", m=None: self.calls.append(("drag", b, round(x), round(y)))
        macos.scroll = lambda dy=0, dx=0: self.calls.append(("scroll", dy))
        macos.frontmost_app = lambda: self.app

    app = "OpenSCAD"


def hand(px, py, label="right"):
    return {"palm": [px, py], "label": label, "index_tip": [px, py]}


async def main() -> int:
    failures = []
    rec = Recorder()
    rec.install()
    cfg = config_module.load()

    print("profile selection")
    for app, expect_orbit in [("OpenSCAD", "left"), ("Blender", "middle"),
                              ("Autodesk Fusion 360", "middle"),
                              ("Google Chrome", "left"), ("Some Unknown App", "middle")]:
        name, prof = cad.profile_for(app)
        ok = prof.orbit[0] == expect_orbit
        print(f"  {'PASS' if ok else 'FAIL'}  {app:24} -> {name:10} orbit={prof.orbit}")
        if not ok:
            failures.append(f"{app} orbit {prof.orbit}")

    print("\norbit drag uses the profile's button and modifiers")
    for app, button, mods in [("OpenSCAD", "left", ()),
                              ("Autodesk Fusion 360", "middle", ("shift",)),
                              ("Blender", "middle", ())]:
        rec.app = app
        rec.calls.clear()
        bus = Bus(); bus.bind_loop(asyncio.get_running_loop())
        mode = cad.CadMode(cfg["cad"], bus, None)
        await mode.enter()
        await mode.on_hand(hand(0.50, 0.50), "pinch", 0.0)     # press
        await mode.on_hand(hand(0.55, 0.50), "pinch", 0.1)     # move right
        down = [c for c in rec.calls if c[0] == "down"]
        drags = [c for c in rec.calls if c[0] == "drag"]
        ok = down and down[0][1] == button and down[0][2] == mods and drags
        print(f"  {'PASS' if ok else 'FAIL'}  {app:24} down={down[:1]} drag={drags[:1]}")
        if not ok:
            failures.append(f"{app} produced {rec.calls}")

    print("\nrelative motion: the cursor is not warped to the hand")
    rec.app = "Blender"; rec.calls.clear(); rec.pos = (900.0, 300.0)
    bus = Bus(); bus.bind_loop(asyncio.get_running_loop())
    mode = cad.CadMode(cfg["cad"], bus, None)
    await mode.enter()
    # Hand travel is filtered and gain-scaled before it reaches the viewport,
    # so the first frame of a move is deliberately damped — the point of the
    # filter is that a hand holding still does not drag. What must hold is the
    # contract: the drag starts from wherever the cursor already was, goes the
    # way the hand went, and converges on the full gain as the move continues.
    hand_x, t = 0.50, 0.0
    await mode.on_hand(hand(hand_x, 0.50), "pinch", t)
    for _ in range(24):                     # a steady sweep, 1% of frame a frame
        hand_x, t = hand_x + 0.01, t + 1 / 24
        await mode.on_hand(hand(hand_x, 0.50), "pinch", t)
    drags = [c for c in rec.calls if c[0] == "drag"]
    first, last = drags[0], drags[-1]
    travelled = last[2] - 900
    ideal = (hand_x - 0.50) * cfg["cad"]["orbit_gain"]
    ok = (first[2] > 900                      # moved the way the hand moved
          and abs(last[3] - 300) <= 2         # and not sideways
          and 0.55 * ideal <= travelled <= ideal)
    print(f"  {'PASS' if ok else 'FAIL'}  anchored at 900,300 -> {last[2]},{last[3]} "
          f"after a {ideal:.0f} px sweep ({100 * travelled / ideal:.0f}% of full gain)")
    if not ok:
        failures.append(f"relative drag went to {last}, ideal {ideal:.0f}")

    # And a hand that is only shaking must not drag at all. This is the whole
    # reason for the filter: unfiltered, 1.2% landmark noise at the orbit gain
    # is about 35 px of drag per frame from a hand that is not moving.
    import random
    random.seed(11)
    rec.app = "Blender"; rec.calls.clear(); rec.pos = (900.0, 300.0)
    bus = Bus(); bus.bind_loop(asyncio.get_running_loop())
    still = cad.CadMode(cfg["cad"], bus, None)
    await still.enter()
    t = 0.0
    for _ in range(60):
        t += 1 / 24
        await still.on_hand(hand(0.5 + random.gauss(0, 0.012),
                                 0.5 + random.gauss(0, 0.012)), "pinch", t)
    shakes = [c for c in rec.calls if c[0] == "drag"]
    worst = max((max(abs(c[2] - 900), abs(c[3] - 300)) for c in shakes), default=0)
    ok = worst <= 60
    print(f"  {'PASS' if ok else 'FAIL'}  a still but shaking hand drifts at most "
          f"{worst} px over 60 frames")
    if not ok:
        failures.append(f"jitter dragged the viewport {worst} px")

    print("\npan uses the pan binding, and a fist leaves the mode")
    rec.app = "Blender"; rec.calls.clear()
    bus = Bus(); bus.bind_loop(asyncio.get_running_loop())
    mode = cad.CadMode(cfg["cad"], bus, None)
    await mode.enter()
    await mode.on_hand(hand(0.5, 0.5), "peace", 0.0)
    await mode.on_hand(hand(0.6, 0.5), "peace", 0.1)
    pan_down = [c for c in rec.calls if c[0] == "down"]
    ok = pan_down and pan_down[0][1] == "middle" and pan_down[0][2] == ("shift",)
    print(f"  {'PASS' if ok else 'FAIL'}  pan -> {pan_down[:1]}")
    if not ok:
        failures.append(f"pan produced {pan_down}")

    await mode.on_hand(hand(0.5, 0.5), "fist", 0.2)
    ok = not mode.active and any(c[0] == "up" for c in rec.calls)
    print(f"  {'PASS' if ok else 'FAIL'}  fist left CAD mode and released the button")
    if not ok:
        failures.append("fist did not exit cleanly")

    print("\ntwo-hand zoom scrolls")
    rec.calls.clear()
    bus = Bus(); bus.bind_loop(asyncio.get_running_loop())
    mode = cad.CadMode(cfg["cad"], bus, None)
    await mode.enter()
    await mode.on_two_hands(hand(0.4, 0.5, "left"), hand(0.6, 0.5), 0.0)
    await mode.on_two_hands(hand(0.3, 0.5, "left"), hand(0.7, 0.5), 1.0)
    scrolls = [c for c in rec.calls if c[0] == "scroll"]
    ok = bool(scrolls) and scrolls[0][1] > 0
    print(f"  {'PASS' if ok else 'FAIL'}  hands apart -> {scrolls[:1]}")
    if not ok:
        failures.append(f"zoom produced {scrolls}")

    print("\n" + ("ALL PASS" if not failures else "FAILURES:\n  " + "\n  ".join(failures)))
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
