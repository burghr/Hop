#!/usr/bin/env bash
# Stop Hop from running at login.
set -e

LABEL="com.burghr.hop"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -f "$PLIST_PATH" ]; then
    echo "Hop isn't installed as a LaunchAgent."
    exit 0
fi

launchctl unload "$PLIST_PATH" 2>/dev/null || true
rm "$PLIST_PATH"

echo "✓ Hop uninstalled from login items."
echo "  (The project files in $(cd "$(dirname "$0")" && pwd) are untouched.)"
echo ""
echo "Still-running Hop process will keep going until you Cmd-Q it."
