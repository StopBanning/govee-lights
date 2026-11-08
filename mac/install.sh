#!/bin/bash
# Builds the two Mac apps and starts them at every login:
#   Lights Hub.app  runs ../govee/lights_page.py (the hub, on port 8737), restarted if it ever stops
#   Lights.app      the menu bar bulb
# `./install.sh remove` stops both and removes their login items. The apps stay in this folder.
set -euo pipefail
cd "$(dirname "$0")"
HERE="$PWD"
AGENTS="$HOME/Library/LaunchAgents"
LOG="$HOME/Library/Logs/govee-lights-hub.log"
HUB=local.govee-lights.hub
MENU=local.govee-lights.menu

remove() {
  for label in "$HUB" "$MENU"; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
  done
}

agent() {  # agent <label> <executable> <keep alive: true|false>
  cat > "$AGENTS/$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$1</string>
  <key>ProgramArguments</key>
  <array>
    <string>$2</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <$3/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>$LOG</string>
  <key>StandardErrorPath</key>
  <string>$LOG</string>
</dict>
</plist>
PLIST
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$1.plist"
}

if [ "${1:-}" = remove ]; then
  remove
  echo "Removed the login items. Delete this folder to remove the apps too."
  exit 0
fi

./build.sh
remove
mkdir -p "$AGENTS"
agent "$HUB" "$HERE/Lights Hub.app/Contents/MacOS/LightsHub" true
agent "$MENU" "$HERE/Lights.app/Contents/MacOS/Lights" false
echo "Installed. macOS will ask whether Lights Hub may find devices on your local network: click Allow."
echo "The hub's log is $LOG"
