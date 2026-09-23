"""Intent routing: what an utterance is actually taken to mean.

Every failure here looks identical from the outside — you say something and
nothing happens — so the cases are written as the sentence said out loud and
the thing that should happen, rather than as regex coverage.

    .venv/bin/python tests/test_intents.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jarvis import config as config_module
from jarvis.ai import intents
from jarvis.models import ModelIndex

FAILURES = []

index = ModelIndex(config_module.load()["models"]["roots"])
intents.model_lookup = index.best
HAS_MODELS = bool(index.scan())


def says(utterance: str, action, note: str = "") -> None:
    """`action` None means: hand it to the model, do not claim it locally."""
    got = intents.match(utterance)
    name = got[0] if got else None
    ok = name == action
    print(f"  {'PASS' if ok else 'FAIL'}  {utterance!r} -> {name}"
          f"{'  (' + note + ')' if note else ''}")
    if not ok:
        FAILURES.append(f"{utterance!r} gave {name}, wanted {action}")


print("\nbeing addressed by name is not part of the request")
# The browser strips the wake word before sending, but a command typed the way
# it would be spoken arrives with it attached — and then nothing matched.
says("jarvis open safari", "open_app")
says("hey jarvis open safari", "open_app")
says("jarvis, open safari", "open_app")
says("jarvis", None, "a name alone asks for nothing")

print("\nopening an app, a URL, a tab")
says("open safari", "open_app")
says("launch spotify", "open_app")
says("open youtube.com", "open_url")
says("open a new tab", "new_tab")
says("pull up cat videos on youtube", "search_web", "a search, not an app")

print("\nthe interface is not a model, however well the name matches")
# There is a glass_window.stl, so these used to put a part on screen.
says("show me the desktop", "show_desktop")
says("close the window", "close_window")

if not HAS_MODELS:
    print("\n  SKIP  model routing — no models indexed on this machine")
else:
    print("\nnaming the format asks for the editor; the bare name asks for the mesh")
    # "open X cad" brings the hologram up in the HUD; only naming the editor
    # ("in openscad") opens the application.
    # "cad" and "scad" are the same request and, spoken, near enough the same
    # sound. Only "scad" was accepted, so "open astrowilly cad" quietly showed
    # a mesh in the HUD instead of opening the CAD application.
    says("open astrowilly cad", "__model")
    # other people talking after the command must not break it
    says("show the worm ok so anyway", "__model")
    # the Mark IV lives in STEP exports; those render too now
    says("show the aft engine", "__model")
    says("open the aft engine step", "__model")
    says("open astrowilly cad and then he said no", "__model")
    says("show me the seed frame yeah whatever", "__model")
    says("open astrowilly scad", "__model")
    says("open astrowilly in cad", "__model")
    says("open astrowilly in openscad", "__open_source")
    says("jarvis open astrowilly cad", "__model")
    says("open astrowilly", "__model")
    says("show me astrowilly", "__model")
    # The name does not have to be exact, or said the way it is spelt.
    says("open astro willy", "__model")
    says("show me the seed frame", "__model")

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED:"))
for failure in FAILURES:
    print(f"  {failure}")
sys.exit(1 if FAILURES else 0)
