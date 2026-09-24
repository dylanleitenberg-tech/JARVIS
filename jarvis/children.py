"""Every process J.A.R.V.I.S. starts, ended when he is.

Closing the window has to mean nothing of his is left running. A child that
outlives him is him, still running, with the window gone: the ollama server
he started stayed up for a day after he closed, and a geometry edit in flight
held the whole process open for minutes after "Powering down", because the
thread waiting on it had to finish before Python would exit.

Each child runs in a process group of its own, so ending it also ends what it
started in turn: the edit bridge's Claude Code, ollama's model runner. `run`
is subprocess.run and `popen` is subprocess.Popen, owned. `stop_all` is
called on the way out (and at interpreter exit, as a backstop), and nothing
new starts after it.

Short, bounded calls (ps, lsof, osascript one-liners) do not need this. The
HUD's own Chrome has its own handling in main.close_hud.
"""
from __future__ import annotations

import atexit
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, List, Set

WINDOWS = sys.platform == "win32"

_lock = threading.Lock()
_live: Set[subprocess.Popen] = set()
_closing = False


class Closing(OSError):
    """Raised instead of starting a process once shutdown has begun."""


def popen(args: Any, **kw: Any) -> subprocess.Popen:
    if WINDOWS:
        kw.setdefault("creationflags", subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kw.setdefault("start_new_session", True)
    with _lock:
        if _closing:
            raise Closing("J.A.R.V.I.S. is shutting down")
        proc = subprocess.Popen(args, **kw)
        _live.add(proc)
    return proc


def release(proc: subprocess.Popen) -> None:
    """Forget a child that has finished."""
    with _lock:
        _live.discard(proc)


def run(args: Any, *, input: Any = None, capture_output: bool = False,
        timeout: float = None, check: bool = False, **kw: Any) -> subprocess.CompletedProcess:
    """subprocess.run, owned. A timeout ends the whole group, not just its head."""
    if input is not None:
        kw["stdin"] = subprocess.PIPE
    if capture_output:
        kw["stdout"] = kw["stderr"] = subprocess.PIPE
    proc = popen(args, **kw)
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except BaseException:
        end(proc, grace=0)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=2)
        raise
    finally:
        release(proc)
    if check and proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, args, out, err)
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def _signal_group(proc: subprocess.Popen, hard: bool) -> None:
    # Only while the head is unreaped: its pid cannot be reused until then.
    if proc.poll() is not None:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        if WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)


def end(proc: subprocess.Popen, grace: float = 3.0) -> None:
    """Ask the group to stop, then insist."""
    _end_all([proc], grace)


def _end_all(procs: List[subprocess.Popen], grace: float) -> None:
    for proc in procs:
        _signal_group(proc, hard=grace <= 0)
    deadline = time.monotonic() + grace
    for proc in procs:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    for proc in procs:
        _signal_group(proc, hard=True)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=2)


def stop_all(grace: float = 3.0) -> int:
    """End every child still running and refuse new ones. Returns how many."""
    global _closing
    with _lock:
        _closing = True
        procs = [p for p in _live if p.poll() is None]
        _live.clear()
    _end_all(procs, grace)
    return len(procs)


# The backstop for every other way out of the interpreter, and for whatever
# imports these modules without going through main's shutdown: a test that
# asked the brain a question used to leave the ollama it started running.
atexit.register(stop_all)
