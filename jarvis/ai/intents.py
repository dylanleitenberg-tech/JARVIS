"""Fast local intent matching.

"Open Safari" should not cost a network round trip. Every utterance is matched
against these patterns first; a hit runs in a few milliseconds, and anything
that does not match falls through to the language model. This also means the
whole system still works with no API key at all, just less conversationally.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

Intent = Tuple[str, Dict[str, Any], str]  # action, args, spoken acknowledgement

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "half": 50, "max": 100,
    "maximum": 100, "full": 100, "all the way up": 100,
}


def _num(text: str, default: int = 50) -> int:
    match = re.search(r"\d+", text)
    if match:
        return int(match.group())
    for word, value in _NUMBER_WORDS.items():
        if word in text:
            return value
    return default


# A wake word at the front, with whatever punctuation follows it. Matches the
# mishearings the config lists too, since those are what actually arrive.
_ADDRESSED = re.compile(r"^(?:hey\s+|ok\s+)?(?:jarvis|javis|jervis|jarvus)\b[\s,.:;!?-]*",
                        re.I)

# (regex, builder) — the builder returns (action, args, acknowledgement).
RULES: List[Tuple[re.Pattern, Any]] = []

# Installed by the host at startup: name -> model, or None. Without it the
# model rules simply never match, and everything else behaves as before.
model_lookup: Optional[Any] = None


def _is_model(name: str) -> bool:
    return bool(model_lookup and name and model_lookup(name))


def rule(pattern: str):
    compiled = re.compile(pattern, re.I)

    def wrap(fn):
        RULES.append((compiled, fn))
        return fn
    return wrap


# ------------------------------------------------------------------ models

@rule(r"^(?:open|load|edit|bring up|pull up)\s+(?:me\s+)?(?:the\s+)?(.+?)\s+"
      r"(?:in|with|using)\s+(open ?scad|fusion|solidworks|blender|onshape)[.!]?$")
def _open_in_cad(m) -> Optional[Intent]:
    app = m.group(2).lower().replace(" ", "")
    return ("__open_source", {"query": m.group(1).strip(), "app": app}, "")


@rule(r"^(?:open|load|show|bring up|pull up)\s+(?:me\s+)?(?:the\s+)?(.+?)"
      r"(?:\s+in)?\s+(?:s?cad|open ?scad)(?:\s+file)?\b(?:[.!,]?\s+.*)?[.!]?$")
def _open_named_cad(m) -> Optional[Intent]:
    """"Open astrowilly cad" — the hologram, in the interface.

    Both "cad" and "scad", because they are the same request and, spoken, the
    same sound: the leading s of "scad" does not survive the d of "astrowilly
    scad" reliably. Either way the model comes up in the HUD viewer, turned by
    hand; the editor is only asked for by name ("open astrowilly in openscad").
    """
    return ("__model", {"query": m.group(1).strip()}, "")


@rule(r"^(?:open|load|edit|bring up|pull up)\s+(?:me\s+)?(?:the\s+)?(.+?)"
      r"(?:\s+in)?\s+(?:step|f3d|source)(?:\s+file)?[.!]?$")
def _open_named_source(m) -> Optional[Intent]:
    """A STEP or Fusion file cannot be drawn in the HUD, so those open the editor."""
    return ("__open_source", {"query": m.group(1).strip(), "app": "openscad"}, "")


@rule(r"^(?:models|model list|show models|what models(?: do i have)?)[.!?]?$")
def _model_list(m) -> Optional[Intent]:
    return ("__model", {"query": ""}, "")


@rule(r"^(?:show|open|view|bring up|pull up|load|display)\s+(?:me\s+)?(?:the\s+)?(.+?)"
      r"(?:\s+(?:model|part|assembly|in 3d))?[.!]?$")
def _show_model(m) -> Optional[Intent]:
    target = m.group(1).strip()
    # Only claim this phrase if a model by that name is actually indexed;
    # otherwise "show me the desktop" and "open Safari" still reach their rules.
    # The recogniser finalises at a pause, and with other people talking the
    # pause comes after their words, so "show the worm ok so anyway" arrives as
    # one utterance: try the phrase, then shorter and shorter from the right.
    words = target.split()
    for n in range(min(len(words), 8), 0, -1):
        candidate = " ".join(words[:n])
        if _is_model(candidate):
            return ("__model", {"query": candidate}, "")
    return None


# ------------------------------------------------------------------- apps

@rule(r"^(?:please\s+)?(?:open|launch|start|fire up|bring up|pull up|run)\s+(?:the\s+)?(?:app\s+)?(.+?)(?:\s+for me)?[.!]?$")
def _open(m) -> Optional[Intent]:
    target = m.group(1).strip()
    if re.match(r"^(?:a\s+)?(?:new\s+)?tab\b", target, re.I):
        return ("new_tab", {}, "New tab.")
    # "pull up cat videos on youtube" is a search, not an app launch.
    if re.search(r"\bon (youtube|google|wikipedia|maps|duckduckgo)\b", target, re.I):
        return None
    # "open youtube.com" / "open github.com/anthropics"
    if re.match(r"^[\w.-]+\.(com|org|net|io|dev|ai|gov|edu|co|me|tv|app)(/\S*)?$", target, re.I):
        return ("open_url", {"url": target}, f"Opening {target}.")
    return ("open_app", {"name": target}, f"Opening {target}.")


@rule(r"^(?:quit|close|kill|shut down|exit)\s+(?:the\s+)?(?:app\s+)?(.+?)[.!]?$")
def _quit(m) -> Optional[Intent]:
    target = m.group(1).strip().lower()
    if target in ("window", "this window", "that", "this"):
        return ("close_window", {}, "Closed.")
    if target in ("tab", "this tab", "the tab"):
        return ("close_tab", {}, "Tab closed.")
    if target in ("everything", "all windows"):
        return None
    return ("quit_app", {"name": target}, f"Quitting {target}.")


@rule(r"^(?:switch to|focus|go to|show me)\s+(?:the\s+)?(.+?)[.!]?$")
def _focus(m) -> Optional[Intent]:
    target = m.group(1).strip().lower()
    if target in ("desktop", "the desktop"):
        return ("show_desktop", {}, "Desktop.")
    if re.match(r"^[\w.-]+\.(com|org|net|io|dev|ai|gov|edu|co|me|tv|app)(/\S*)?$", target):
        return ("open_url", {"url": target}, f"Opening {target}.")
    if re.match(r"^(?:space|desktop)\s*\d", target):
        return ("switch_space", {"index": _num(target, 1)}, f"Space {_num(target, 1)}.")
    if re.match(r"^tab\s*\d", target):
        return ("select_tab", {"index": _num(target, 1)}, f"Tab {_num(target, 1)}.")
    return ("focus_app", {"name": target}, f"Switching to {target}.")


@rule(r"^(?:hide|minimi[sz]e)\s*(?:the\s+)?(.*)$")
def _hide(m) -> Optional[Intent]:
    target = m.group(1).strip()
    if not target or target in ("window", "this", "this window"):
        return ("minimize_window", {}, "Minimised.")
    return ("hide_app", {"name": target}, f"Hiding {target}.")


# ------------------------------------------------------------------- web

@rule(r"^(?:search|google|look up|find)\s+(?:for\s+)?(.+?)(?:\s+on\s+(google|youtube|wikipedia|duckduckgo|maps))?[.!?]?$")
def _search(m) -> Optional[Intent]:
    query, engine = m.group(1).strip(), (m.group(2) or "google").lower()
    return ("search_web", {"query": query, "engine": engine},
            f"Searching for {query}.")


@rule(r"^(?:play|find|pull up)\s+(.+?)\s+on\s+youtube[.!]?$")
def _youtube(m) -> Optional[Intent]:
    return ("search_web", {"query": m.group(1).strip(), "engine": "youtube"},
            f"Searching YouTube for {m.group(1).strip()}.")


@rule(r"^(?:go to|navigate to|visit)\s+(.+?)[.!]?$")
def _goto(m) -> Optional[Intent]:
    return ("open_url", {"url": m.group(1).strip().replace(" dot ", ".").replace(" ", "")},
            "On screen.")


@rule(r"^(?:new tab|open a new tab)[.!]?$")
def _newtab(m) -> Optional[Intent]:
    return ("new_tab", {}, "New tab.")


@rule(r"^(?:what(?:'s| is)\s+)?(?:open|my tabs|list (?:my )?tabs)[.!?]?$")
def _tabs(m) -> Optional[Intent]:
    return ("browser_tabs", {}, "")


# ---------------------------------------------------------------- windows

@rule(r"^(?:close|dismiss)(?:\s+(?:this|the|that))?\s*window[.!]?$")
def _closewin(m) -> Optional[Intent]:
    return ("close_window", {}, "Closed.")


@rule(r"^(?:snap|move|put|dock)\s+(?:this\s+|the\s+)?window\s+(?:to\s+(?:the\s+)?)?(left|right|top|bottom|centre|center|full(?:screen)?)[.!]?$")
def _snap(m) -> Optional[Intent]:
    where = m.group(1).lower().replace("centre", "center").replace("fullscreen", "full")
    return ("snap_window", {"where": where}, f"Snapped {where}.")


@rule(r"^(?:full ?screen|maximi[sz]e)(?:\s+this)?(?:\s+window)?[.!]?$")
def _full(m) -> Optional[Intent]:
    return ("fullscreen_window", {}, "Fullscreen.")


@rule(r"^(?:mission control|show (?:me )?(?:all )?(?:my )?windows|expos[eé])[.!]?$")
def _mc(m) -> Optional[Intent]:
    return ("mission_control", {}, "")


@rule(r"^(?:show(?: me)? the )?desktop[.!]?$")
def _desktop(m) -> Optional[Intent]:
    return ("show_desktop", {}, "")


@rule(r"^(?:space|desktop)\s*(\d)[.!]?$")
def _space(m) -> Optional[Intent]:
    return ("switch_space", {"index": int(m.group(1))}, f"Space {m.group(1)}.")


@rule(r"^tab\s*(\d)[.!]?$")
def _tabn(m) -> Optional[Intent]:
    return ("select_tab", {"index": int(m.group(1))}, "")


# ------------------------------------------------------------------ media

@rule(r"^(?:set )?volume\s*(?:to|at)?\s*(\d{1,3}|half|max(?:imum)?|full)[%\s]*[.!]?$")
def _vol(m) -> Optional[Intent]:
    level = _num(m.group(1), 50)
    return ("set_volume", {"level": level}, f"Volume {level}.")


@rule(r"^(?:turn (?:the )?)?volume\s*(up|down)[.!]?$")
def _volstep(m) -> Optional[Intent]:
    step = 12 if m.group(1).lower() == "up" else -12
    return ("volume_step", {"delta": step}, "")


@rule(r"^(?:mute|silence)(?:\s+(?:the\s+)?(?:audio|sound|volume))?[.!]?$")
def _mute(m) -> Optional[Intent]:
    return ("mute", {"muted": True}, "Muted.")


@rule(r"^unmute(?:\s+.*)?[.!]?$")
def _unmute(m) -> Optional[Intent]:
    return ("mute", {"muted": False}, "Sound restored.")


@rule(r"^(?:play|pause|resume|play or pause)(?:\s+(?:the\s+)?(?:music|media|song|video|track))?[.!]?$")
def _play(m) -> Optional[Intent]:
    return ("play_pause", {}, "")


@rule(r"^(?:next|skip)(?:\s+(?:the\s+)?(?:track|song))?[.!]?$")
def _next(m) -> Optional[Intent]:
    return ("next_track", {}, "")


@rule(r"^(?:previous|back|last)(?:\s+(?:track|song))[.!]?$")
def _prev(m) -> Optional[Intent]:
    return ("prev_track", {}, "")


@rule(r"^(?:turn (?:the )?)?brightness\s*(up|down)[.!]?$")
def _bright(m) -> Optional[Intent]:
    return ("set_brightness", {"direction": m.group(1).lower(), "steps": 3},
            f"Brightness {m.group(1).lower()}.")


# ----------------------------------------------------------------- system

@rule(r"^(?:take a |grab a )?(?:screenshot|screen ?capture|screen ?grab)(?: of the screen)?[.!]?$")
def _shot(m) -> Optional[Intent]:
    return ("screenshot", {}, "Captured to the desktop.")


@rule(r"^lock(?: the)?(?: screen| mac| computer)?[.!]?$")
def _lock(m) -> Optional[Intent]:
    return ("lock_screen", {}, "Locking up.")


@rule(r"^(?:system )?(?:status|diagnostics|report|how are we doing|systems check)[.!?]?$")
def _status(m) -> Optional[Intent]:
    return ("system_status", {}, "")


@rule(r"^(?:what(?:'s| is) (?:my |the )?battery(?: at| level)?|battery(?: status| level)?)[.!?]?$")
def _battery(m) -> Optional[Intent]:
    return ("system_status", {}, "")


@rule(r"^(?:copy that|copy to clipboard)[.!]?$")
def _copy(m) -> Optional[Intent]:
    return ("press_key", {"combo": "cmd+c"}, "Copied.")


@rule(r"^paste[.!]?$")
def _paste(m) -> Optional[Intent]:
    return ("press_key", {"combo": "cmd+v"}, "Pasted.")


@rule(r"^(?:press|hit|key)\s+(.+?)[.!]?$")
def _key(m) -> Optional[Intent]:
    combo = (m.group(1).lower()
             .replace("command", "cmd").replace(" plus ", "+")
             .replace(" and ", "+").replace(" ", "+"))
    if not re.fullmatch(r"[a-z0-9+]+", combo):
        return None
    return ("press_key", {"combo": combo}, "")


@rule(r"^(?:type|write|enter)\s+(.+)$")
def _type(m) -> Optional[Intent]:
    return ("type_text", {"text": m.group(1)}, "")


@rule(r"^run (?:the )?shortcut\s+(.+?)[.!]?$")
def _shortcut(m) -> Optional[Intent]:
    return ("run_shortcut", {"name": m.group(1).strip()}, "")


# -------------------------------------------------------------- assistant

CHITCHAT = {
    r"^(?:hello|hi|hey|good morning|good evening|good afternoon)\b":
        "Good to see you, Sir. Systems are nominal.",
    r"^(?:thank you|thanks|cheers|nice work|well done)\b":
        "At your service.",
    r"^(?:are you (?:there|online|awake)|you there)\b":
        "Always, Sir.",
    r"^(?:who are you|what are you)\b":
        "J.A.R.V.I.S. — Just A Rather Very Intelligent System. Running locally on this machine.",
    r"^(?:what can you do|help|what are my options)\b":
        "I can open and close applications, manage windows and tabs, search, control media and "
        "volume, take screenshots, read system telemetry, and follow your hands. Ask plainly.",
}

CONTROL_PHRASES = [
    (r"^(?:gesture|hand)s?\s*(?:control|tracking|mode)?\s*(?:on|enable[d]?|up)\b",
     ("__arm_gestures", {"armed": True}, "Gesture control armed.")),
    (r"^(?:gesture|hand)s?\s*(?:control|tracking|mode)?\s*(?:off|disable[d]?|down)\b",
     ("__arm_gestures", {"armed": False}, "Gesture control stood down.")),
    # Anchored to the end of the utterance: "shut down" powers J.A.R.V.I.S.
    # down, but "shut down Spotify" must still reach the quit_app rule.
    (r"^(?:cad|c\.?a\.?d\.?)\s*(?:mode)?\s*(?:on|start|enable)?[.!]?$",
     ("__cad", {"active": True}, "")),
    (r"^(?:exit|leave|stop|end)\s+(?:cad|c\.?a\.?d\.?)\s*(?:mode)?[.!]?$",
     ("__cad", {"active": False}, "Leaving CAD mode.")),
    (r"^(?:cad|c\.?a\.?d\.?)\s*(?:mode)?\s*(?:off|stop|disable)[.!]?$",
     ("__cad", {"active": False}, "Leaving CAD mode.")),
    (r"^(?:fit|zoom to fit|frame it|fit to screen)[.!]?$",
     ("__cad_fit", {}, "")),
    (r"^(?:shut ?down|power down|power off|quit|exit)(?: jarvis)?[.!]?$",
     ("__quit", {}, "Powering down. Good day, Sir.")),
    (r"^goodbye(?:,? jarvis)?[.!]?$",
     ("__quit", {}, "Powering down. Good day, Sir.")),
    (r"^(?:yes|yeah|confirm(?:ed)?|do it|proceed|affirmative|go ahead)\b",
     ("__confirm", {"accept": True}, "")),
    (r"^(?:no|cancel|stop|nevermind|never mind|belay that|negative|abort)\b",
     ("__confirm", {"accept": False}, "Cancelled.")),
]


def match(text: str) -> Optional[Intent]:
    """Return (action, args, acknowledgement) for an utterance, or None."""
    cleaned = text.strip().strip(".,!?").strip()
    # The browser strips the wake word before sending, but a command typed the
    # way it would be spoken — "jarvis open astrowilly cad" — arrives with it
    # attached, and then nothing matches at all. Addressing something by name
    # is not part of the request.
    cleaned = _ADDRESSED.sub("", cleaned, count=1).strip()
    if not cleaned:
        return None

    for pattern, intent in CONTROL_PHRASES:
        if re.match(pattern, cleaned, re.I):
            return intent

    for pattern, reply in CHITCHAT.items():
        if re.match(pattern, cleaned, re.I):
            return ("__say", {"text": reply}, reply)

    for compiled, builder in RULES:
        found = compiled.match(cleaned)
        if found:
            intent = builder(found)
            if intent is not None:
                return intent
    return None
