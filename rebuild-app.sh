#!/bin/bash
# Rebuild and reinstall JARVIS.app.
#
# READ THIS BEFORE RUNNING: every rebuild produces a new ad-hoc signature, and
# macOS stores an Accessibility grant against the exact signature. Rebuilding
# therefore SILENTLY REVOKES control permission — the entry still looks ticked
# and does nothing. That cost most of an afternoon. Only run this when the
# launcher itself must change, and re-grant afterwards.
set -e
cd "$(dirname "$0")"
clang -O2 -framework ApplicationServices -framework CoreGraphics -o JARVIS.app/Contents/MacOS/jarvis src/launcher.c
codesign --force --deep --sign - JARVIS.app
rm -rf /Applications/JARVIS.app
cp -R JARVIS.app /Applications/
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f /Applications/JARVIS.app
codesign -dv --verbose=4 /Applications/JARVIS.app 2>&1 | grep "CandidateCDHash sha256" | awk '{print $NF}' > .bundle-hash
echo
echo "  Rebuilt. EVERY macOS grant for this bundle is now STALE —"
echo "  Accessibility AND Camera AND Microphone. macOS binds a grant to the"
echo "  exact signature, and the rebuild changed it."
echo
echo "  for s in Accessibility Camera Microphone; do"
echo "    tccutil reset \$s com.leitenberg.jarvis; done"
echo
echo "  then ./check-control.sh, and allow each prompt."
