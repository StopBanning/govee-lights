#!/bin/bash
# Builds the two small Mac apps next to this script, using the Swift compiler from Apple's command line tools:
#   Lights Hub.app  runs ../govee/lights_page.py, so macOS asks about local network access for "Lights Hub"
#   Lights.app      the menu bar button, which talks to the hub on port 8737
# Rebuilding changes the apps' ad-hoc signatures, so macOS may ask about Lights Hub's network access again.
# `./build.sh menu` rebuilds only Lights.app, which needs no such permission; `./build.sh hub` only Lights Hub.
set -euo pipefail
cd "$(dirname "$0")"

build() {  # build <app name> <executable> <swift file> <Info.plist>
  local app="$1.app"
  rm -rf "$app"
  mkdir -p "$app/Contents/MacOS"
  cp "$4" "$app/Contents/Info.plist"
  swiftc -O -swift-version 5 -o "$app/Contents/MacOS/$2" "$3"
  codesign --force --sign - "$app"
  echo "built $PWD/$app"
}

case "${1:-all}" in
  hub) build "Lights Hub" LightsHub LightsHub.swift LightsHub-Info.plist ;;
  menu) build "Lights" Lights Lights.swift Lights-Info.plist ;;
  all)
    build "Lights Hub" LightsHub LightsHub.swift LightsHub-Info.plist
    build "Lights" Lights Lights.swift Lights-Info.plist
    ;;
  *) echo "usage: build.sh [hub|menu]" >&2; exit 2 ;;
esac
