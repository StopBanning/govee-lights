#!/bin/bash
# Builds the two small Mac apps next to this script, using the Swift compiler from Apple's command line tools:
#   Lights Hub.app  runs ../govee/lights_page.py, so macOS asks about local network access for "Lights Hub"
#   Lights.app      the menu bar button, which talks to the hub on port 8737
#   Govee Bluetooth.app  carries commands to Bluetooth-only lights; macOS asks once about Bluetooth for it
# Rebuilding changes the apps' ad-hoc signatures, so macOS may ask about Lights Hub's network access again.
# `./build.sh menu` rebuilds only Lights.app, which needs no such permission; `./build.sh hub` only Lights Hub;
# `./build.sh bluetooth` only Govee Bluetooth (which may then ask about Bluetooth again).
set -euo pipefail
cd "$(dirname "$0")"

build() {  # build <app name> <executable> <swift file> <Info.plist> [login item label]
  local app="$1.app"
  rm -rf "$app"
  mkdir -p "$app/Contents/MacOS"
  cp "$4" "$app/Contents/Info.plist"
  swiftc -O -swift-version 5 -o "$app/Contents/MacOS/$2" "$3"
  codesign --force --sign - "$app"
  echo "built $PWD/$app"
  # A copy still running from the old files gets killed by macOS ("code signature invalid") the next time it
  # loads code from them, so restart it now if it's installed as a login item.
  local label="${5:-}"
  if [ -n "$label" ] && launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    launchctl kickstart -k "gui/$(id -u)/$label" && echo "restarted $label"
  fi
}

case "${1:-all}" in
  hub) build "Lights Hub" LightsHub LightsHub.swift LightsHub-Info.plist local.govee-lights.hub ;;
  menu) build "Lights" Lights Lights.swift Lights-Info.plist local.govee-lights.menu ;;
  bluetooth) build "Govee Bluetooth" GoveeBluetooth GoveeBluetooth.swift GoveeBluetooth-Info.plist ;;
  all)
    build "Lights Hub" LightsHub LightsHub.swift LightsHub-Info.plist local.govee-lights.hub
    build "Lights" Lights Lights.swift Lights-Info.plist local.govee-lights.menu
    build "Govee Bluetooth" GoveeBluetooth GoveeBluetooth.swift GoveeBluetooth-Info.plist
    ;;
  *) echo "usage: build.sh [hub|menu|bluetooth]" >&2; exit 2 ;;
esac
