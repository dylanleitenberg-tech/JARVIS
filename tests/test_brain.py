"""Brain backend tests: selection, degradation, and a live round trip.

Backend selection is where J.A.R.V.I.S. goes quiet without saying why — a
missing key, an unpulled model, a CLI that is not installed all used to look
identical from the HUD. These check that each one reports itself accurately.

The round trip at the end runs only against whatever backend is configured and
actually reachable, so this stays useful with no model installed at all.

    .venv/bin/python tests/test_brain.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jarvis import config as config_module
from jarvis.ai.brain import Brain, _spoken_only

FAILURES = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        FAILURES.append(label)


class FakeBus:
    def __init__(self):
        self.events = []

    async def publish(self, kind, **fields):
        self.events.append((kind, fields))


class FakeDispatcher:
    def tool_specs(self):
        return []

    async def run(self, name, args, source=""):
        return {"ok": True, "result": "done"}


def brain_for(backend: str, **over) -> Brain:
    cfg = config_module.load()
    cfg["ai"]["backend"] = backend
    cfg["ai"].update(over)
    return Brain(cfg, FakeBus(), FakeDispatcher())


# ------------------------------------------------------- reasoning scratchpad

print("\na reasoning model's scratchpad is never spoken")
check("think block stripped",
      _spoken_only("<think>I should check the time.</think>Half past four."),
      "Half past four.")
check("plain text untouched", _spoken_only("Half past four."), "Half past four.")
check("truncated block takes the rest with it",
      _spoken_only("<think>cut off by max_tokens"), "")
check("mid-sentence block", _spoken_only("A<thinking>x</thinking>B"), "AB")
check("non-string content", _spoken_only(None), "")


# ------------------------------------------------------- backend degradation

print("\nevery backend says which of its failures it is in")
offline = brain_for("offline").info()
check("offline is honest about having no model", offline["status"], "offline")

ollama = brain_for("ollama", model="claude-sonnet-5").info()
# Switching backend alone must not leave an API model name in place, or the
# advice becomes `ollama pull claude-sonnet-5`, which is not a thing.
check("an API model name is not carried into ollama",
      "claude" in ollama["model"].lower(), False)
print(f"        ollama reports: {ollama['status']} — {ollama['detail']}")

cc = brain_for("claude-code").info()
# Either outcome is correct; what matters is that it is decided now rather
# than at the first spoken question.
check("claude-code resolves at startup, not at first use",
      cc["status"] in ("ready", "offline"), True)
print(f"        claude-code reports: {cc['status']} — {cc['detail']}")

nokey = brain_for("anthropic", api_key_env="JARVIS_DEFINITELY_UNSET").info()
# To a local model when one is installed (the "openai" client talks to
# ollama), else a Claude Code login its owner switched on, else offline.
check("no key falls through rather than going silently mute",
      nokey["backend"] in ("openai", "command", "offline"), True)
print(f"        anthropic without a key: {nokey['backend']} — {nokey['detail']}")


# ------------------------------------------------------------- live round trip

print("\nlive round trip against the configured backend")
brain = Brain(config_module.load(), FakeBus(), FakeDispatcher())
info = brain.info()
if info["status"] != "ready":
    print(f"  SKIP  {info['backend']} is not ready ({info['detail']})")
else:
    reply = asyncio.run(brain.ask("Say the single word: online. Nothing else."))
    print(f"        {info['backend']} ({info['detail']}) said: {reply!r}")
    check("a reply came back", bool(reply.strip()), True)
    check("no scratchpad leaked into it", "<think" in reply.lower(), False)
    # The system prompt says one or two sentences because it is spoken aloud.
    check("short enough to speak", len(reply) < 400, True)


# ------------------------------------------------------------- tool selection

class SpyDispatcher(FakeDispatcher):
    """The real 42 action schemas, but nothing actually runs."""

    def __init__(self):
        from jarvis.control.actions import Dispatcher
        self._real = Dispatcher(config_module.load())
        self.calls = []

    def tool_specs(self):
        return self._real.tool_specs()

    async def run(self, name, args, source=""):
        self.calls.append((name, args))
        return {"ok": True, "result": "ok"}


print("\npicking one action out of the whole registry")
if info["status"] != "ready":
    print(f"  SKIP  {info['backend']} is not ready")
else:
    import time

    # Only utterances the intent parser does NOT claim reach the model, but
    # these are unambiguous, which is the point: a model that cannot place
    # these has no chance at the phrasings that do fall through.
    for utterance, expect in [("turn the volume down to twenty", "set_volume"),
                              ("take a screenshot", "screenshot"),
                              ("what applications are running", "list_apps")]:
        spy = SpyDispatcher()
        clock = time.monotonic()
        asyncio.run(Brain(config_module.load(), FakeBus(), spy).ask(utterance))
        elapsed = time.monotonic() - clock
        check(f"{utterance!r} -> {expect} ({elapsed:.1f}s)",
              [c[0] for c in spy.calls], [expect])
        # A spoken reply is something you wait through. Past about ten seconds
        # you assume it did not hear you and say it again — which is how the
        # local model was found deliberating for eight seconds before acting.
        check(f"  answered inside 12s ({elapsed:.1f}s)", elapsed < 12.0, True)

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"))
sys.exit(1 if FAILURES else 0)
