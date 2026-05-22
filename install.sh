#!/usr/bin/env bash
# Install Hop as a LaunchAgent so it starts automatically at login.
set -e
cd "$(dirname "$0")"

PROJECT_DIR="$(pwd)"
LABEL="com.burghr.hop"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PROJECT_DIR/run.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/hop.out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/hop.err.log</string>
</dict>
</plist>
EOF

# Reload if already installed
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo "✓ Hop installed. It will start automatically at login."
echo "  Plist: $PLIST_PATH"
echo "  Logs:  /tmp/hop.out.log  /tmp/hop.err.log"
echo ""
echo "To uninstall, run: ./uninstall.sh"
