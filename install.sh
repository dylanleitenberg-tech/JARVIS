#!/bin/sh
# J.A.R.V.I.S. installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/dylanleitenberg-tech/JARVIS/main/install.sh | sh
#
# Installs into your own folders (no sudo): the code, a private Python from
# uv, and a launcher (JARVIS.app in ~/Applications on a Mac, a menu entry on
# Linux). Running it again updates the code and keeps your settings, logs and
# caches. Nothing is asked for here: JARVIS asks for camera, microphone and
# control permissions itself, in its setup panel, the first time it opens.
#
# Options (or the matching environment variable):
#   --dir PATH       install location              JARVIS_HOME
#   --source PATH    install from a local checkout JARVIS_SOURCE
#   --branch NAME    branch to download            JARVIS_BRANCH (main)
#   --with-step      also install CadQuery for STEP files (about 1 GB)
#   --no-shortcuts   no app bundle, menu entry or `jarvis` command
#   --no-launch      do not start it at the end
#   --uninstall      remove it (your model files are never touched)
set -eu

REPO="dylanleitenberg-tech/JARVIS"
BRANCH="${JARVIS_BRANCH:-main}"
SOURCE="${JARVIS_SOURCE:-}"
WITH_STEP=0
SHORTCUTS=1
LAUNCH=1
UNINSTALL=0

OS="$(uname -s)"
case "$OS" in
  Darwin) DEFAULT_HOME="$HOME/Library/Application Support/JARVIS" ;;
  Linux)  DEFAULT_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/jarvis" ;;
  *) echo "This installer is for macOS and Linux. On Windows use install.ps1." >&2; exit 1 ;;
esac
DEST="${JARVIS_HOME:-$DEFAULT_HOME}"

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DEST="$2"; shift ;;
    --source) SOURCE="$2"; shift ;;
    --branch) BRANCH="$2"; shift ;;
    --with-step) WITH_STEP=1 ;;
    --no-shortcuts) SHORTCUTS=0 ;;
    --no-launch) LAUNCH=0 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,24p' "$0" 2>/dev/null || true; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

say()  { printf '  %s\n' "$*"; }
step() { printf '\n  \033[36m▸\033[0m %s\n' "$*"; }
die()  { printf '\n  \033[31m✗\033[0m %s\n\n' "$*" >&2; exit 1; }

APP="$HOME/Applications/JARVIS.app"
DESKTOP_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/applications/jarvis.desktop"
BIN="$HOME/.local/bin/jarvis"

# ------------------------------------------------------------------ uninstall
if [ "$UNINSTALL" = 1 ]; then
  step "Removing J.A.R.V.I.S."
  pkill -f "jarvis.main" 2>/dev/null || true
  [ "$OS" = Darwin ] && rm -rf "$APP" && say "removed $APP"
  rm -f "$DESKTOP_FILE" "$BIN"
  rm -rf "$DEST" && say "removed $DEST"
  if [ "$OS" = Darwin ]; then
    for s in Accessibility Camera Microphone AppleEvents ScreenCapture; do
      tccutil reset "$s" com.leitenberg.jarvis >/dev/null 2>&1 || true
    done
    say "cleared its macOS permissions"
  fi
  printf '\n  Done. Your model files were not touched.\n\n'
  exit 0
fi

printf '\n  J.A.R.V.I.S. — installing into %s\n' "$DEST"

fetch() {  # fetch URL FILE
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$2" "$1"
  else die "needs curl or wget"; fi
}

# ----------------------------------------------------------------------- code
step "Getting the code"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
if [ -n "$SOURCE" ]; then
  SRC="$SOURCE"
  say "from $SRC"
else
  fetch "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" "$TMP/src.tar.gz" \
    || die "could not download $REPO ($BRANCH)"
  tar -xzf "$TMP/src.tar.gz" -C "$TMP"
  SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
fi
[ -f "$SRC/jarvis/main.py" ] || die "$SRC does not look like J.A.R.V.I.S."

mkdir -p "$DEST"
# Code is replaced; what belongs to the person is kept: settings, logs, the
# browser profile holding the microphone grant, the Python, build caches.
for item in jarvis web bridge tests packaging src requirements.txt README.md install.sh install.ps1; do
  if [ -e "$SRC/$item" ]; then
    rm -rf "$DEST/$item"
    cp -R "$SRC/$item" "$DEST/$item"
  fi
done
find "$DEST/jarvis" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
mkdir -p "$DEST/logs"

# ------------------------------------------------------------------- python
step "Setting up Python"
UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ]; then
  UV="$DEST/.uv/uv"
  [ -x "$UV" ] || UV="$DEST/.uv/bin/uv"
  if [ ! -x "$UV" ]; then
    say "installing uv (a Python installer) into $DEST/.uv"
    fetch "https://astral.sh/uv/install.sh" "$TMP/uv.sh"
    env UV_INSTALL_DIR="$DEST/.uv" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 \
      sh "$TMP/uv.sh" >/dev/null 2>&1 || die "could not install uv"
    UV="$DEST/.uv/uv"
    [ -x "$UV" ] || UV="$DEST/.uv/bin/uv"
    [ -x "$UV" ] || die "uv installed, but not where expected ($DEST/.uv)"
  fi
fi
PY="$DEST/.venv/bin/python"
if [ ! -x "$PY" ]; then
  "$UV" venv --quiet --python 3.12 "$DEST/.venv" || die "could not create the Python environment"
fi

step "Installing packages (a few minutes the first time)"
grep -v '^mediapipe' "$DEST/requirements.txt" > "$TMP/core.txt"
"$UV" pip install --quiet --python "$PY" -r "$TMP/core.txt" || die "package install failed"
VISION=1
if ! "$UV" pip install --quiet --python "$PY" "$(grep '^mediapipe' "$DEST/requirements.txt")"; then
  VISION=0
  say "hand tracking (MediaPipe) is not available for this machine; voice and typing still work"
fi

if [ "$WITH_STEP" = 1 ]; then
  step "Installing CadQuery for STEP files"
  [ -x "$DEST/.step-env/bin/python" ] || "$UV" venv --quiet --python 3.12 "$DEST/.step-env"
  "$UV" pip install --quiet --python "$DEST/.step-env/bin/python" cadquery \
    || say "CadQuery did not install; STEP files will be listed but not drawn"
fi

# A first config, only if there is none: hand tracking off where it could not install.
if [ ! -f "$DEST/jarvis.json" ] && [ "$VISION" = 0 ]; then
  printf '{\n  "vision": {"enabled": false}\n}\n' > "$DEST/jarvis.json"
fi

# ---------------------------------------------------------------- shortcuts
if [ "$SHORTCUTS" = 1 ]; then
  step "Adding the launcher"
  mkdir -p "$HOME/.local/bin"
  cat > "$BIN" <<EOF
#!/bin/sh
cd "$DEST" && exec "$PY" -m jarvis.main "\$@"
EOF
  chmod +x "$BIN"
  say "command: $BIN"

  if [ "$OS" = Darwin ]; then
    # The app is what macOS grants camera, microphone and Accessibility to,
    # so it is signed here, on this machine, and remembers where the code is.
    mkdir -p "$HOME/Applications"
    rm -rf "$APP"
    cp -R "$DEST/packaging/macos/JARVIS.app" "$APP"
    printf '%s\n' "$DEST" > "$APP/Contents/Resources/jarvis-root"
    xattr -cr "$APP" 2>/dev/null || true
    codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || say "(could not sign the app bundle)"
    say "app: $APP"
  else
    mkdir -p "$(dirname "$DESKTOP_FILE")"
    cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=J.A.R.V.I.S.
Comment=Voice and hand-gesture assistant
Exec="$PY" -m jarvis.main --log
Path=$DEST
Icon=$DEST/packaging/linux/jarvis.png
Terminal=false
Categories=Utility;
EOF
    say "menu entry: $DESKTOP_FILE"
  fi
fi

# ------------------------------------------------------------------ extras
if [ "$OS" = Linux ]; then
  MISSING=""
  for tool in wmctrl xdotool playerctl pactl notify-send; do
    command -v "$tool" >/dev/null 2>&1 || MISSING="$MISSING $tool"
  done
  if [ -n "$MISSING" ]; then
    say ""
    say "Optional tools for window, media and sound control are missing:$MISSING"
    say "e.g.  sudo apt install wmctrl xdotool playerctl pulseaudio-utils libnotify-bin"
  fi
  if [ "${XDG_SESSION_TYPE:-}" = wayland ]; then
    say "You are on Wayland: JARVIS cannot type or click into other apps there."
    say "Choose an Xorg session at login for full control."
  fi
fi

BROWSER_OK=0
if [ "$OS" = Darwin ]; then
  for b in "/Applications/Google Chrome.app" "$HOME/Applications/Google Chrome.app" "/Applications/Microsoft Edge.app"; do
    [ -d "$b" ] && BROWSER_OK=1
  done
else
  for b in google-chrome google-chrome-stable microsoft-edge microsoft-edge-stable; do
    command -v "$b" >/dev/null 2>&1 && BROWSER_OK=1
  done
fi
[ "$BROWSER_OK" = 1 ] || say "For voice, install Google Chrome or Microsoft Edge (their pages can recognise speech)."

printf '\n  \033[32m✓\033[0m Installed.\n'
say "It asks for camera, microphone and control permissions when it opens."
say "Remove it any time:  sh \"$DEST/install.sh\" --uninstall"

# --------------------------------------------------------------------- launch
if [ "$LAUNCH" = 1 ]; then
  step "Starting J.A.R.V.I.S."
  if [ "$OS" = Darwin ] && [ "$SHORTCUTS" = 1 ]; then
    open "$APP"
  else
    (cd "$DEST" && nohup "$PY" -m jarvis.main --log >/dev/null 2>&1 &)
  fi
fi
printf '\n'
