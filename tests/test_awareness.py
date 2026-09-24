"""Memory, project knowledge, and the rules that keep him quiet.

The watcher tests are all about silence. An assistant that reports every
observation is worse than one that never speaks, because you stop listening,
and then it cannot tell you the one thing that mattered.

    .venv/bin/python tests/test_awareness.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from jarvis import config as config_module
from jarvis.knowledge import Knowledge
from jarvis.memory import Memory
from jarvis.watch import Watcher

FAILURES = []


def check(label, got, want=True) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        got {got!r}, wanted {want!r}")
        FAILURES.append(label)


# ------------------------------------------------------------------- memory

print("\nmemory survives a restart and does not accumulate duplicates")
store = pathlib.Path(tempfile.mkdtemp()) / "memory.json"
m = Memory(store)
check("a fresh memory adds nothing to the prompt", m.brief(), "")

m.remember("The aft engine assembly is a STEP export with no parameters.")
m.remember("Name parts in plain words, never by index.")
m.remember("the AFT ENGINE ASSEMBLY is a step export with no parameters!!")
check("restating a fact replaces it", len(m.facts), 2)

m.note("model", "looking at the aft engine assembly")
m.note("edit", "widening the bore to 12 mm")
m.note("edit", "widening the bore to 12 mm")
check("a repeated episode collapses", len(m.episodes), 2)
check("and is counted", m.episodes[-1]["count"], 2)

m.save()
check("it reloads from disk", (len(Memory(store).facts), len(Memory(store).episodes)), (2, 2))
check("forgetting matches on a phrase", m.forget("never by index"), 1)
check("and only that one", len(m.facts), 1)
# Case-insensitively: a restated fact replaces the original, so what is
# stored is however the user said it the last time, not the first.
check("what is remembered reaches the prompt", "step export" in m.brief().lower())


# ---------------------------------------------------------------- knowledge

print("\nthe project's own notes, offered only when they fit")
k = Knowledge(config_module.load()["models"]["roots"])
docs = k.scan()
if not docs:
    print("  SKIP  no project documents on this machine")
else:
    print(f"        {len(docs)} documents across "
          f"{len(set(d['project'] for d in docs))} projects")
    check("nothing on screen offers nothing", k.brief(), "")
    k.set_focus(docs[0]["project"])
    check("the focused project's notes are offered", bool(k.brief("make it longer")))
    k.set_focus("Project_That_Does_Not_Exist")
    check("an unknown project offers nothing", k.brief("make it longer"), "")


# ------------------------------------------------------------------ silence

print("\nspeaking up: the rules are all about NOT speaking")
HEALTHY = dict(battery=80, charging=False, disk_free_gb=500, vision_online=True,
               had_vision=True, can_control=True, had_control=True,
               brain_ready=True, brain_was_ready=True, unsaved_for=0, busy=False)


def watcher(gap: float = 0.0) -> Watcher:
    w = Watcher(config_module.load())
    w.last_spoke, w.floor = 0.0, gap
    return w


def say(w, **over):
    return w.tick({**HEALTHY, **over}, now=time.time())


w = watcher()
check("a healthy machine says nothing", say(w), None)
check("a condition BECOMING true speaks", bool(say(w, battery=18)))
check("the same condition does not speak again", say(w, battery=18), None)
check("nor as it gets worse", say(w, battery=17), None)

w = watcher()
check("nothing interrupts an edit", say(w, disk_free_gb=3, busy=True), None)
# Suppression must leave the watch armed, or a condition that arrives during
# an edit and does not go away — low disk does not go away — is silent for
# good.
check("but it speaks once you are free", bool(say(w, disk_free_gb=3)))
check("and then not again", say(w, disk_free_gb=3), None)

w = watcher()
check("something urgent does interrupt", bool(say(w, battery=8, busy=True)))

w = watcher()
say(w, disk_free_gb=3, busy=True)
check("what came and went during an edit is not news after it",
      say(w, disk_free_gb=500), None)

w = watcher()
check("a camera that never worked has not been lost",
      say(w, vision_online=False, had_vision=False), None)
check("one that was working has", bool(say(w, vision_online=False)))

w = watcher(gap=90.0)
check("two problems at once produce one remark", bool(say(w, battery=8, disk_free_gb=2)))
check("and the floor holds the second back",
      say(w, battery=8, disk_free_gb=2, vision_online=False), None)

w = Watcher({**config_module.load(), "watch": {"enabled": False}})
check("it can be switched off entirely", say(w, battery=5), None)

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED:"))
for f in FAILURES:
    print(f"  {f}")
sys.exit(1 if FAILURES else 0)
