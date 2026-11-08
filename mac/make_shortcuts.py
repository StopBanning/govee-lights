#!/usr/bin/env python3
"""Makes the light Shortcuts (Lights Warm, Bright, Dim, Off) as signed .shortcut files in ./shortcuts.

Each one sends a POST to the lights hub at http://<this Mac's name>.local:8737, so it works from the Mac and, once
iCloud syncs it, from the iPhone on the home Wi-Fi. Open a file to import it, then show the shortcuts in the
Shortcuts widget on the desktop or the Home Screen.
"""
import os
import plistlib
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True,
                      check=True).stdout.strip() + ".local"
# Scene, and an icon color from the Shortcuts palette (RGBA packed into one number).
SCENES = [("warm", 4271458815), ("bright", 4274264319), ("dim", 2071128575), ("off", 255)]


def shortcut(scene: str, color: int) -> dict:
    return {
        "WFWorkflowClientVersion": "1200",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": color, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowTypes": [],
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowImportQuestions": [],
        "WFWorkflowActions": [{
            "WFWorkflowActionIdentifier": "is.workflow.actions.downloadurl",
            "WFWorkflowActionParameters": {"WFURL": f"http://{HOST}:8737/api/scene/{scene}", "WFHTTPMethod": "POST"},
        }],
    }


def main() -> None:
    out = os.path.join(HERE, "shortcuts")
    os.makedirs(out, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for scene, color in SCENES:
            name = f"Lights {scene.title()}.shortcut"
            unsigned = os.path.join(tmp, name)
            with open(unsigned, "wb") as f:
                plistlib.dump(shortcut(scene, color), f, fmt=plistlib.FMT_BINARY)
            signed = os.path.join(out, name)
            subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", unsigned, "--output", signed],
                           check=True, capture_output=True)
            print("made", signed)


if __name__ == "__main__":
    main()
