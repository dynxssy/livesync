#!/bin/bash
# LiveSync installer for macOS. Double-click this file (or run it in Terminal).
set -e
HERE="$(cd "$(dirname "$0")/.." && pwd)"
UL="${LIVESYNC_USER_LIBRARY:-$HOME/Music/Ableton/User Library}"
echo "Installing LiveSync into: $UL"
mkdir -p "$UL/Remote Scripts" "$UL/Presets/Audio Effects/Max Audio Effect"
rm -rf "$UL/Remote Scripts/LiveSync"
cp -R "$HERE/remote_script/LiveSync" "$UL/Remote Scripts/LiveSync"
find "$UL/Remote Scripts/LiveSync" -name "__pycache__" -type d -prune -exec rm -rf {} +
cp "$HERE/m4l/LiveSync.amxd" "$UL/Presets/Audio Effects/Max Audio Effect/LiveSync.amxd"
mkdir -p "$HOME/Documents/LiveSync"
echo ""
echo "Done. Next:"
echo "  1. Restart Ableton Live."
echo "  2. Live > Settings > Link, Tempo & MIDI > Control Surface: choose LiveSync (Input/Output: None)."
echo "  3. Drop 'LiveSync' (Browser > Audio Effects > Max Audio Effect) on your Master track,"
echo "     or open http://127.0.0.1:47010 in your web browser."
read -n 1 -s -r -p "Press any key to close."
