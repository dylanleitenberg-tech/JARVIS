#!/bin/bash
# Prove control, through the installed app bundle — the identity macOS grants.
#
# Running the bundle's inner executable from a shell does NOT give it the
# bundle's identity; only a LaunchServices launch does. So this uses `open`,
# which is also how the assistant itself must be started.
cd "$(dirname "$0")" || exit 1
APP=/Applications/JARVIS.app
[ -d "$APP" ] || APP="$PWD/JARVIS.app"
rm -f logs/control-check.txt
# -n forces a NEW instance. The normal run now stays resident to serve
# control requests, and without -n LaunchServices would simply activate
# that one and drop these arguments on the floor.
open -n -a "$APP" --args --check-control
for _ in $(seq 1 40); do
    [ -s logs/control-check.txt ] && break
    sleep 0.25
done
cat logs/control-check.txt 2>/dev/null || echo "  the app did not report back — see logs/run.log"
