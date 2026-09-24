"""Noticing things, and deciding almost none of them are worth saying.

Everything needed to speak unprompted was already here — telemetry every few
seconds, a voice, a bus. What was missing is the only hard part: judgement. An
assistant that reports every observation is worse than one that never speaks,
because you stop listening to it, and then it cannot tell you the one thing
that mattered.

So the rules here are about silence:

  a watch fires on a CONDITION BECOMING TRUE, never on it being true. Battery
  passing 15% is news; battery being at 14% is not, and saying so every thirty
  seconds is how an assistant gets muted.

  nothing speaks twice. Once said, a watch stays quiet until its condition
  clears and comes back, and then not for `repeat_after` seconds either way.

  nothing speaks while you are talking, thinking, or mid-edit. An interruption
  during a CAD change is worse than a late warning.

  a hard ceiling on how often anything at all may speak, so a combination
  nobody anticipated cannot produce a monologue.

Each watch is a function of the latest telemetry plus a little context. It
returns a sentence, or None. That is the whole interface.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

# Never more than one unprompted remark in this window, whatever is going on.
FLOOR = 90.0
# And never the same watch twice inside this one, even if it re-triggers.
REPEAT_AFTER = 1800.0


class Watch:
    def __init__(self, name: str, test: Callable[[dict], Optional[str]],
                 repeat_after: float = REPEAT_AFTER, urgent: bool = False):
        self.name = name
        self.test = test
        self.repeat_after = repeat_after
        self.urgent = urgent      # urgent watches ignore the "is he busy" gate
        self.was_true = False
        self.last_said = 0.0

    def check(self, state: dict, now: float) -> Optional[str]:
        """The line this watch wants to say, or None. Does not commit to
        having said it — `spoke` does that, once the caller has decided the
        moment is right."""
        try:
            line = self.test(state)
        except Exception:
            return None
        if not line:
            self.was_true = False        # cleared: it may speak again later
            return None
        if self.was_true:
            return None                  # still true, already said
        if now - self.last_said < self.repeat_after:
            return None
        return line

    def spoke(self, now: float) -> None:
        self.was_true = True
        self.last_said = now


class Watcher:
    def __init__(self, config: dict):
        cfg = config.get("watch", {})
        self.enabled = bool(cfg.get("enabled", True))
        self.floor = float(cfg.get("min_gap", FLOOR))
        self.last_spoke = time.time()    # start quiet, not with a greeting
        self.watches: List[Watch] = []
        self._build(cfg)

    # ------------------------------------------------------------- watches

    def _build(self, cfg: dict) -> None:
        low = float(cfg.get("battery_low", 20))
        very_low = float(cfg.get("battery_critical", 10))
        disk = float(cfg.get("disk_low_gb", 12))

        def battery(s):
            b, charging = s.get("battery"), s.get("charging")
            if b is None or charging:
                return None
            if b <= very_low:
                return f"Battery at {b:.0f} percent, Sir. You will want the charger."
            if b <= low:
                return f"Battery is down to {b:.0f} percent."
            return None

        def disk_space(s):
            free = s.get("disk_free_gb")
            if free is None or free > disk:
                return None
            return f"Disk space is down to {free:.0f} gigabytes."

        def camera_lost(s):
            # Only once the camera has been working, or this fires at startup
            # every time before the first frame arrives.
            if not s.get("had_vision") or s.get("vision_online"):
                return None
            return "I have lost the camera. Gesture control is down until it comes back."

        def control_lost(s):
            if not s.get("had_control") or s.get("can_control"):
                return None
            return "I have lost control permissions. Nothing I do to the mouse will land."

        def model_down(s):
            if not s.get("brain_was_ready") or s.get("brain_ready"):
                return None
            return "My connection to the model has gone. I am on fixed commands only."

        def unsaved(s):
            """The one that is genuinely useful: edits live in memory and are
            lost on close until saved."""
            mins = s.get("unsaved_for")
            if not mins or mins < float(cfg.get("unsaved_after_min", 12)):
                return None
            return (f"You have had unsaved changes for {mins:.0f} minutes. "
                    f"Say save when you want them kept.")

        self.watches = [
            Watch("battery", battery, urgent=True),
            Watch("disk", disk_space),
            Watch("camera", camera_lost),
            Watch("control", control_lost),
            Watch("model", model_down),
            Watch("unsaved", unsaved, repeat_after=900.0),
        ]

    # -------------------------------------------------------------- ticking

    def tick(self, state: dict, now: Optional[float] = None) -> Optional[str]:
        """The one line worth saying right now, or None. Usually None."""
        if not self.enabled:
            return None
        now = now or time.time()

        # Nothing is marked as said until it is actually said. An earlier
        # version latched the trigger when it was suppressed for being busy,
        # which meant a condition that arrived mid-edit and was still true
        # afterwards — low disk, say, which does not go away — stayed silent
        # for good. Suppression has to leave the watch armed.
        busy = bool(state.get("busy"))
        ready = []
        for watch in self.watches:
            line = watch.check(state, now)
            if line and (watch.urgent or not busy):
                ready.append((watch.urgent, line, watch))

        if not ready or now - self.last_spoke < self.floor:
            return None
        ready.sort(key=lambda r: not r[0])       # anything urgent first
        _, line, watch = ready[0]
        watch.spoke(now)
        self.last_spoke = now
        return line

    def status(self) -> Dict[str, object]:
        return {"enabled": self.enabled,
                "watching": [w.name for w in self.watches],
                "armed": [w.name for w in self.watches if not w.was_true]}
