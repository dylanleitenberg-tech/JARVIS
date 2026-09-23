#!/usr/bin/env python3
"""Start J.A.R.V.I.S. in its own session so it outlives the shell that spawned it.

A plain `nohup ... &` still shares the caller's process group, so when that
group is torn down the backend gets a signal and shuts itself down. Starting a
new session detaches it properly.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
log = open(ROOT / "logs" / "run.log", "ab", buffering=0)
proc = subprocess.Popen(
    [str(ROOT / ".venv" / "bin" / "python"), "-u", "-m", "jarvis.main", *sys.argv[1:]],
    cwd=str(ROOT), stdout=log, stderr=log, stdin=subprocess.DEVNULL,
    start_new_session=True,          # the whole point: a new process group
)
print(f"jarvis started, pid {proc.pid}")
