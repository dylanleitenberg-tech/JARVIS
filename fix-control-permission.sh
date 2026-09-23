#!/bin/bash
# Make this venv's Python able to hold an Accessibility grant.
#
# .venv/bin/python3 is normally a symlink to Apple's signed com.apple.python3.
# macOS treats an Apple "platform binary" as its own responsible process, so it
# never inherits the grant given to the terminal that launched it — which is
# why ticking Terminal, or jarvis-run, changes nothing.
#
# Replacing the symlink with a copy and re-signing it ad-hoc makes it an
# ordinary binary, and TCC then attributes it to the app that launched it.
#
# Reverse with: ./fix-control-permission.sh --undo
set -e
cd "$(dirname "$0")"
PY=.venv/bin/python3

if [ "$1" = "--undo" ]; then
    REAL=$(cat .venv/bin/.python3.origin 2>/dev/null || echo "")
    [ -z "$REAL" ] && { echo "no saved original; nothing to undo"; exit 1; }
    rm -f "$PY" && ln -s "$REAL" "$PY"
    echo "restored the symlink to $REAL"
    exit 0
fi

if [ -L "$PY" ]; then
    REAL=$(python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "$PY")
    echo "$REAL" > .venv/bin/.python3.origin
    echo "original: $REAL"
    REAL_DIR=$(dirname "$REAL")
    rm -f "$PY"
    cp "$REAL" "$PY"
    echo "copied the interpreter into the venv"
else
    echo "already a real file, re-signing it"
fi

# The interpreter finds its runtime via @executable_path/../Python3, so that
# path has to exist beside the copy or it will not start at all.
FW=$(dirname "$(dirname "$REAL_DIR")")/Python3
if [ ! -e .venv/Python3 ]; then
    FRAMEWORK=$(python3 - <<'PYEOF'
import pathlib, sys, subprocess
real = subprocess.run(["python3","-c","import sys;print(sys.base_prefix)"],
                      capture_output=True, text=True).stdout.strip()
cand = pathlib.Path(real) / "Python3"
print(cand if cand.exists() else "")
PYEOF
)
    [ -n "$FRAMEWORK" ] && ln -sf "$FRAMEWORK" .venv/Python3 && echo "linked .venv/Python3 -> $FRAMEWORK"
fi

codesign --force --sign - "$PY"
echo
echo "signature now:"
codesign -dv --verbose=2 "$PY" 2>&1 | grep -iE "identifier|flags" | sed 's/^/  /'
echo
echo "Next:"
echo "  1. Quit Terminal with Cmd-Q and open it again."
echo "  2. cd ~/JARVIS && ./jarvis-run --check-control"
echo "  3. If macOS prompts, allow it. If not, add Terminal under"
echo "     Privacy & Security > Accessibility and try again."
