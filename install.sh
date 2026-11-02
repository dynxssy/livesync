#!/bin/bash
# LiveSync one-line installer for macOS:
#   curl -fsSL https://raw.githubusercontent.com/dynxssy/livesync/main/install.sh | bash
# (private repo: gh api repos/dynxssy/livesync/contents/install.sh -H "Accept: application/vnd.github.raw" | bash)
set -euo pipefail
REPO="dynxssy/livesync"
SERVER="${LIVESYNC_SERVER:-wss://livesync-relay.onrender.com}"
UL="${LIVESYNC_USER_LIBRARY:-$HOME/Music/Ableton/User Library}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "→ Downloading LiveSync…"
if ! curl -fsSL "https://codeload.github.com/$REPO/tar.gz/main" -o "$TMP/livesync.tgz" 2>/dev/null; then
  if command -v gh >/dev/null 2>&1 && gh api "repos/$REPO/tarball/main" > "$TMP/livesync.tgz" 2>/dev/null; then :; else
    echo "✗ Couldn't download $REPO. If the repo is private, ask its owner to add you, then run 'gh auth login' and try again."
    exit 1
  fi
fi
mkdir -p "$TMP/src"
tar -xzf "$TMP/livesync.tgz" -C "$TMP/src" --strip-components 1

if [ ! -d "$UL" ]; then
  echo "✗ Ableton User Library not found at: $UL"
  echo "  Run again with LIVESYNC_USER_LIBRARY=\"/path/to/User Library\" in front of the command."
  exit 1
fi

echo "→ Installing into $UL"
mkdir -p "$UL/Remote Scripts" "$UL/Presets/Audio Effects/Max Audio Effect"
rm -rf "$UL/Remote Scripts/LiveSync"
cp -R "$TMP/src/remote_script/LiveSync" "$UL/Remote Scripts/LiveSync"
cp "$TMP/src/m4l/LiveSync.amxd" "$UL/Presets/Audio Effects/Max Audio Effect/LiveSync.amxd"

echo "→ Setting relay server to $SERVER"
CFG_DIR="$HOME/Documents/LiveSync"
CFG="$CFG_DIR/settings.json"
mkdir -p "$CFG_DIR"
if [ -f "$CFG" ] && /usr/bin/plutil -replace server -string "$SERVER" "$CFG" 2>/dev/null; then :; else
  printf '{\n  "server": "%s"\n}\n' "$SERVER" > "$CFG"
fi

echo ""
echo "✓ LiveSync installed. Last steps in Ableton Live:"
echo "  1. Quit and reopen Live."
echo "  2. Settings › Link, Tempo & MIDI › Control Surface: pick LiveSync (Input/Output: None)."
echo "  3. Drag LiveSync (Browser › Audio Effects › Max Audio Effect) onto the Master track."
echo "  4. Open an empty Live Set, type your partner's code and press Join."
