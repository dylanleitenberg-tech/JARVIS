"""Configuration: defaults, jarvis.json overrides, environment overrides."""
from __future__ import annotations

import copy
import json
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "jarvis.json"

DEFAULTS = {
    "server": {
        "host": "127.0.0.1",
        "port": 8420,
        "open_browser": True,
        "kiosk": True,
        # The HUD is meant to stay up. Closing the window asks first, and if it
        # goes away anyway it is reopened — unless you powered down deliberately.
        "persist_hud": True,
        "relaunch_after": 8.0,     # grace period, so a reload is not a close
        "max_relaunches": 6,       # then stop, rather than fight the user
    },
    "speech": {
        # STT and TTS both run in the browser (Web Speech API). The page owns the
        # microphone, so it can mute recognition while J.A.R.V.I.S. is speaking and
        # never transcribe its own voice.
        "wake_words": ["jarvis", "javis", "jervis", "jarvus"],
        "sleep_words": ["that will be all", "stand down", "go to sleep"],
        "always_listening": True,
        # Seconds of silence after a command before the utterance is dispatched.
        "command_timeout": 6.0,
        # Empty means "pick the best available" — Google's network voices
        # first, then a premium or enhanced British male, then Daniel. Naming
        # one here overrides the ranking entirely. The stock Mac set is poor;
        # System Settings > Accessibility > Spoken Content > System Voice >
        # Manage Voices has far better ones, and they are free.
        "voice_hint": "",
        # Slower than speech normally is, because an assistant that sounds
        # unhurried sounds certain. But NOT pitched down: a speech synthesiser
        # shifts pitch by resampling, and on a basic voice — which is all this
        # Mac has, with no premium voice installed — that does not deepen it,
        # it makes it warble. Depth has to come from the voice itself, and
        # this is the setting to leave alone until a better one is installed.
        "rate": 0.92,
        "pitch": 1.0,
        "volume": 1.0,
        "locale": "en-US",
    },
    "vision": {
        "enabled": True,
        "camera_index": 0,
        "width": 960,
        "height": 540,
        "fps": 30,
        "mirror": True,
        "hands": True,
        "pose": True,
        "face": True,
        "max_hands": 2,
        "min_detection_confidence": 0.6,
        "min_tracking_confidence": 0.5,
        "stream_feed": True,     # serve the annotated camera feed to the HUD
        "feed_quality": 55,
        "feed_width": 480,
    },
    "gestures": {
        "enabled": True,
        # Gestures only drive the machine once armed, so a stray wave cannot quit
        # an app. Say "gesture control on" or hold an open palm for `arm_hold`.
        "require_arm": True,
        "arm_hold": 1.2,
        # Smallest wrist-to-middle-MCP span, as a fraction of frame height, that
        # counts as a hand. Below this it is background clutter the tracker fit
        # landmarks to, and it must never arm or click.
        "min_hand_scale": 0.07,
        # Only applies to gesture control armed by holding a palm up, and only
        # with no hand seen at all for this long. Switching it on deliberately
        # keeps it on until you switch it off.
        "disarm_after_idle": 300.0,
        # Thumb-index gap as a fraction of hand scale (wrist to middle MCP), so
        # the same numbers hold near and far from the lens. Hysteresis pair: it
        # takes a tighter pinch to start one than to keep holding it.
        "pinch_on": 0.30,
        "pinch_off": 0.45,
        "swipe_velocity": 1.4,  # screen widths per second
        "cooldown": 0.65,       # seconds between discrete gesture fires
        # Only applies if you deliberately bind a gesture to "disarm", which
        # the defaults no longer do.
        "disarm_hold": 0.9,
        "cursor_smoothing": 0.35,
        "scroll_gain": 900.0,   # screen-heights of scroll per frame-height moved
        # The defaults follow one rule: a gesture does what the same hand shape
        # does on a trackpad, and nothing destructive is reachable by accident.
        # Anything irreversible lives in the radial menu, where you must aim at
        # it and pinch to confirm.
        "bindings": {
            # pointer
            "point": "cursor",              # index out, like a mouse
            "pinch": "click",               # thumb + index; hold and move to drag
            "pinch_drag": "drag",
            "pinch_middle": "right_click",  # thumb + middle, the second button
            "peace": "scroll",              # two fingers, like a trackpad
            "two_hand_spread": "zoom",
            # navigation
            "swipe_left": "next_app",
            "swipe_right": "prev_app",
            "swipe_up": "mission_control",
            "swipe_down": "show_desktop",
            # control
            "open_palm_hold": "radial_menu",
            # Nothing a hand does turns gesture control OFF. Every closed-hand
            # shape is also a RESTING shape — a chin propped on a fist, a hand
            # leaving frame, a hand mid-transition — so binding "off" to one
            # meant it switched itself off while you sat still. Off is the
            # switch, the g key, or "gesture control off".
            "fist": None,
            "thumbs_up": "confirm",
            "thumbs_down": "dismiss",
            # The ring and little finger pinch unreliably, so they are left
            # unbound rather than given something that half-works.
            "pinch_ring": None,
            "pinch_pinky": None,
        },
    },
    "models": {
        # Roots searched for STL/OBJ models shown in the HUD's own 3D viewer.
        # Everything served is resolved against this allow-list.
        "roots": ["~/Asteroid_Miner", "~/Terrestrial_Replicator",
                  "~/AR_Glasses_Rig", "~/FTC_BIOBUZZ"],
        "max_files": 4000,
    },
    "cad": {
        # Hand-driven viewport navigation. Say "CAD mode" to enter, make a fist
        # or say "exit CAD mode" to leave. Movement is RELATIVE: put the cursor
        # in the viewport once, then the hand only supplies deltas.
        "orbit_gain": 1600.0,   # screen pixels per frame-width of hand travel
        "pan_gain": 1400.0,
        "zoom_gain": 260.0,
        # MediaPipe's palm landmark wanders one or two percent of frame width
        # with a hand held still, which at the gain above is twenty-odd pixels
        # of drag per frame from a hand that is not moving. These control the
        # one-euro filter that removes it. Lower cutoff = steadier and laggier.
        # Measured against synthetic hands at 24 fps with 1.2% landmark noise:
        # a held-still hand drags 35 px/frame unfiltered, 2.5 px at these
        # values, and a deliberate slow nudge goes from 13x more noise than
        # signal to 2x more signal than noise. Beta is what keeps it from
        # being merely a laggy low-pass — at 0.6/0.008 the still hand is
        # steadier still, but the model keeps turning for ten frames after you
        # stop, and lag is worse than shake.
        "smoothing_cutoff": 0.6,
        "smoothing_beta": 4.0,     # how fast smoothing gives way to speed
        # Pointer acceleration. One gain cannot serve both "turn it round to
        # see the back" and "nudge it five degrees", so a slow hand gets
        # min_gain_scale of the gain and a hand at precision_speed (frame
        # widths per second) gets all of it.
        "precision_speed": 0.30,
        "min_gain_scale": 0.20,
    },
    "ai": {
        # backend: anthropic | ollama | claude-code | openai | command | offline
        #
        #   anthropic    best answers, needs ANTHROPIC_API_KEY, costs money.
        #                Without the key it falls through to claude-code rather
        #                than going silently mute.
        #   ollama       runs on this machine. No key, no account, no network,
        #                no session open anywhere else, and about a second to
        #                answer. Needs `ollama pull <local_model>` once.
        #   claude-code  borrows the Claude Code CLI login. No key either, but
        #                it is a subprocess per question — a few seconds.
        "backend": "anthropic",
        "model": "claude-sonnet-5",
        # Used instead of `model` when the backend is ollama, so switching
        # backends does not mean editing the model name too.
        "local_model": "qwen3:8b",
        # The model that writes geometry edits, through the Claude Code login.
        # Opus by default: this is the one call that has to reason about shape
        # rather than words — which named solids "the nozzle skirt" covers,
        # what is attached to what, which anchor keeps a joint closed. It is
        # slower, and worth it, because a wrong edit costs a rebuild and an
        # undo. "sonnet" if you want the speed back.
        # Pinned rather than the "opus" alias: the alias follows the latest
        # Opus, and a geometry editor changing under you is how a setup that
        # worked yesterday starts producing different edits today. Set it to
        # "opus" to track the newest instead.
        "edit_model": "claude-opus-5-5",
        "edit_timeout": 300.0,
        # The understanding step — anything the fixed phrases miss. Sonnet,
        # because this reply is SPOKEN: measured on the same utterance it
        # answers in 6.3 s where Opus 5.5 takes 14.4, and fourteen seconds of
        # silence is long enough that you say it again. Set it to
        # "claude-opus-5-5" if you would rather wait for a better answer.
        "smart_model": "sonnet",
        # After an edit applies, photograph the model in the HUD and let the
        # model look at what it made before the user is told it worked. This
        # is the difference between a tool that guesses and one that checks:
        # a bounding box cannot see a part left floating or geometry burst
        # into spikes. Costs one extra call and a few seconds; a bad edit that
        # gets through costs a rebuild and an undo.
        "verify_edits": True,
        "verify_settle": 1.6,      # seconds for the viewer to rebuild first
        # How long a local reasoning model may deliberate before answering:
        # none | low | medium | high. Defaults to "none" on ollama, where
        # thinking is the difference between a 1.4 s reply and an 8 s one.
        "reasoning_effort": None,
        # Seconds of silence after which a local model is unloaded, handing
        # back the ~6 GB an 8B model holds resident. Reloading costs about
        # 2.5 s, so it stays put through a conversation and goes away when you
        # stop talking. 0 keeps it loaded (ollama's own timeout still applies).
        "unload_after": 120.0,
        "max_tokens": 1024,
        "api_key_env": "ANTHROPIC_API_KEY",
        "base_url": None,          # openai-compatible: e.g. http://localhost:11434/v1
        "command": None,           # backend=command: argv list, prompt on stdin
        "history_turns": 12,
        "allow_tools": True,
        "system": (
            "You are J.A.R.V.I.S., Tony Stark's assistant, now running on the user's Mac. "
            "You are dry, unflappable, and economical. Address the user as 'Sir' occasionally, "
            "not every sentence. Your replies are SPOKEN ALOUD, so: one or two sentences, no "
            "markdown, no lists, no emoji, no stage directions. When the user asks you to do "
            "something on the computer, call the matching tool and then confirm in a few words. "
            "If a request is ambiguous, ask one short question. Never narrate what you are about to do."
        ),
    },
    "watch": {
        # Speaking without being spoken to. The hard part is not noticing —
        # the telemetry loop has always had these numbers — it is staying
        # quiet. A watch fires when a condition BECOMES true, never while it
        # is true, and nothing speaks twice until it clears and comes back.
        "enabled": True,
        "min_gap": 90.0,            # never two unprompted remarks inside this
        "battery_low": 20.0,
        "battery_critical": 10.0,
        "disk_low_gb": 12.0,
        "unsaved_after_min": 12.0,  # edits live in memory until you say save
    },
    "safety": {
        # Actions that need a spoken or gestural confirmation before they run.
        "confirm_actions": ["quit_app", "lock_screen", "sleep_display", "run_shell"],
        "allow_shell": False,
        "blocked_apps": [],
    },
    "hud": {
        "theme": "stark",           # stark (cyan) | mark42 (gold) | combat (red)
        "scanlines": True,
        "grain": True,
        "bloom": True,
        "boot_sequence": True,
        "show_feed": True,
        "reduce_motion": False,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: pathlib.Path | None = None) -> dict:
    path = path or CONFIG_PATH
    user = {}
    if path.exists():
        try:
            user = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path} is not valid JSON: {exc}") from exc
    cfg = _deep_merge(DEFAULTS, user)

    # Environment escape hatches, handy for one-off runs.
    if os.environ.get("JARVIS_PORT"):
        cfg["server"]["port"] = int(os.environ["JARVIS_PORT"])
    if os.environ.get("JARVIS_NO_VISION"):
        cfg["vision"]["enabled"] = False
    if os.environ.get("JARVIS_NO_BROWSER"):
        cfg["server"]["open_browser"] = False
    if os.environ.get("JARVIS_MODEL"):
        cfg["ai"]["model"] = os.environ["JARVIS_MODEL"]
    if os.environ.get("JARVIS_AI_BACKEND"):
        cfg["ai"]["backend"] = os.environ["JARVIS_AI_BACKEND"]
    return cfg


def save_patch(patch: dict, path: pathlib.Path | None = None) -> pathlib.Path:
    """Merge a patch into jarvis.json on disk, keeping the rest untouched.

    Only what the user has actually overridden is stored, so defaults stay
    live and a later change to DEFAULTS still reaches them.
    """
    path = path or CONFIG_PATH
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text())
        except json.JSONDecodeError:
            current = {}
    path.write_text(json.dumps(_deep_merge(current, patch), indent=2) + "\n")
    return path


def write_default(path: pathlib.Path | None = None) -> pathlib.Path:
    """Write a commented-by-example config the user can edit."""
    path = path or CONFIG_PATH
    if not path.exists():
        path.write_text(json.dumps(DEFAULTS, indent=2) + "\n")
    return path
