"""Closing J.A.R.V.I.S. ends everything he started, and nothing else.

    .venv/bin/python tests/test_children.py
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jarvis import children

FAILURES = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name} {detail}")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); ask ps whether it is really running.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def gone_within(pid: int, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


# A child that starts a grandchild and prints its pid: the case that matters,
# since the edit bridge starts Claude Code and ollama starts its runner.
SPAWNER = ["/bin/sh", "-c", "sleep 60 & echo $!; wait"]

if sys.platform == "win32":
    print("  SKIP  process groups are POSIX here")
    sys.exit(0)

r = children.run([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                 input="hello", capture_output=True, text=True, timeout=30)
check("run returns what subprocess.run would", r.returncode == 0 and r.stdout.strip() == "HELLO",
      repr(r.stdout))

r = children.run([sys.executable, "-c", "import sys; sys.exit(3)"], capture_output=True)
check("a failing child reports its exit code", r.returncode == 3, str(r.returncode))

p = children.popen(SPAWNER, stdout=subprocess.PIPE, text=True)
grandchild = int(p.stdout.readline())
try:
    children.run(["/bin/sh", "-c", "sleep 60"], timeout=0.3)
    check("a timeout is raised", False)
except subprocess.TimeoutExpired:
    check("a timeout is raised", True)
children.end(p, grace=1.0)
check("ending a child ends its grandchild", gone_within(grandchild, 3), f"pid {grandchild}")
p.stdout.close()

# Anything that imports jarvis and exits without main's shutdown (a test, a
# tool) still takes its children with it.
code = ("import subprocess, sys; sys.path.insert(0, %r); from jarvis import children; "
        "p = children.popen(%r, stdout=subprocess.PIPE, text=True); "
        "print(p.pid, p.stdout.readline().strip(), flush=True)"
        % (str(pathlib.Path(__file__).resolve().parents[1]), SPAWNER))
out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
pids = [int(x) for x in out.stdout.split()]
check("an interpreter that exits ends its children", len(pids) == 2
      and gone_within(pids[0], 3) and gone_within(pids[1], 3), out.stdout + out.stderr)

# Not his: started outside children, it must survive his shutdown.
stranger = subprocess.Popen(["/bin/sh", "-c", "sleep 60"])
owned = children.popen(SPAWNER, stdout=subprocess.PIPE, text=True)
owned_grandchild = int(owned.stdout.readline())
term_ignorer = children.popen([sys.executable, "-c",
                               "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                               "print('ready', flush=True); time.sleep(60)"],
                              stdout=subprocess.PIPE, text=True)
term_ignorer.stdout.readline()

t0 = time.time()
count = children.stop_all(grace=1.0)
took = time.time() - t0
check("stop_all ends every owned child", count == 2 and owned.poll() is not None
      and term_ignorer.poll() is not None, f"count {count}")
check("and their grandchildren", gone_within(owned_grandchild, 3), f"pid {owned_grandchild}")
check("one that ignores SIGTERM is killed after the grace", took < 5, f"{took:.1f} s")
check("a process that is not his is left alone", stranger.poll() is None)
stranger.kill()
stranger.wait()
owned.stdout.close()
term_ignorer.stdout.close()

try:
    children.popen(["/bin/sh", "-c", "true"])
    check("nothing new starts once shutdown has begun", False)
except children.Closing:
    check("nothing new starts once shutdown has begun", True)

print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED"))
sys.exit(1 if FAILURES else 0)
