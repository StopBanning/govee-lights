# govee-lights

Control Govee lights from a Mac over the LAN, without the Govee app: a menu bar app, a phone web page, Shortcuts,
a CLI, and an MCP server for Claude, Codex and other agents.

Uses Govee's LAN API (UDP, no cloud, no key), and Bluetooth for lights that don't have it. Standard-library Python
plus three small Swift apps; nothing to install.

## Requirements

- macOS 13+ (the Python parts also run on Linux)
- Python 3.9+
- `swiftc` for the Mac apps (`xcode-select --install`; Xcode not needed)
- **LAN Control** on for each light: Govee Home app → the light → gear icon

## CLI

```bash
python3 govee/govee_lan.py list
python3 govee/govee_lan.py set --temp warm
python3 govee/govee_lan.py set bulbs --color blue --brightness 40
python3 govee/govee_lan.py blink "bulb 3"
python3 govee/govee_lan.py name "bulb 3" "desk lamp" --room office
python3 govee/govee_lan.py set office --power off
```

Targets: `all` (default), a name, a room, a kind (`bulbs`, `strips`, `floor lamps`, `light bars`...), a label
(`strip 2`), an IP or a device ID; comma-separate several. On first run macOS asks for Local Network access; allow it.

## Finding lights

- Every model with LAN Control is found, whatever it is. Known models get labels like `strip 1` or `panel 2`;
  others show as `light 1`. Add yours to `KINDS` in `govee/govee_lan.py`.
- The scan goes out on every network the Mac is on, so a VPN or a second interface doesn't hide the lights.
- If your router blocks multicast (some mesh systems and guest networks do), list the lights' IPs:
  `GOVEE_LIGHT_IPS="192.168.1.20,192.168.1.21"`.
- Bluetooth-only lights are found too, on a Mac (see below).
- With a Govee API key set (see Scenes), `list` names unnamed lights after the names in the Govee Home app, and
  lists any light in your account that didn't answer, which usually means LAN Control is off for it.

## Bluetooth lights

Govee lights without LAN Control (many strips, like the H617A) are controlled over Bluetooth from the Mac, through
**Govee Bluetooth.app**, which `mac/build.sh` builds. macOS asks once whether it may use Bluetooth: allow it. After
that they show up in `list` (`strip 1 ... over Bluetooth`) and work everywhere the other lights do: the CLI, the
menu bar, the phone page, Shortcuts, MCP, and Govee's scenes, which go over Bluetooth with no API key.

- Power, brightness, color and scenes. White temperature is sent as the nearest RGB color.
- Power and brightness are read back to confirm a change; color can't be.
- Range is the Mac's Bluetooth range, and each command connects first, so allow 2-3 seconds.
- A light takes one Bluetooth connection at a time: while the Govee app on a phone is connected to it, commands
  from the Mac can't get in.
- Wi-Fi lights advertise over Bluetooth too. Ones already reached over the network are skipped.

Packet format: see `govee/govee_ble.py`. Tested on an H617A; other Bluetooth models may need a different color
command (send a PR).

## Mac apps

```bash
./mac/install.sh          # build both apps and add login items
./mac/install.sh remove   # remove the login items
```

- **Lights Hub** runs `govee/lights_page.py`, an HTTP hub on port 8737 that everything else goes through. Allow it
  when macOS asks about the local network. It's a separate app because launchd blocks bare Python from the LAN
  without prompting.
- **Govee Bluetooth** carries commands to Bluetooth-only lights (see above).
- **Lights** is the menu bar app: presets, brightness and temperature sliders, colors and Govee scenes, per kind
  and per room.

Restart the hub after editing the Python: `launchctl kickstart -k gui/$(id -u)/local.govee-lights.hub`.
Log: `~/Library/Logs/govee-lights-hub.log`.

**Phone page:** `http://<mac-name>.local:8737`, private networks only. **Shortcuts:**
`python3 mac/make_shortcuts.py` writes signed Warm/Bright/Dim/Off shortcuts to `mac/shortcuts/`.

## MCP

Tools: `list_lights`, `set_lights`, `identify_light`, `name_light`, `list_scenes`, `start_scene`.

```bash
claude mcp add --scope user govee -- python3 "$PWD/govee/govee_mcp.py"
codex mcp add govee -- python3 "$PWD/govee/govee_mcp.py"
```

Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{ "mcpServers": { "govee": { "command": "python3", "args": ["/path/to/govee-lights/govee/govee_mcp.py"] } } }
```

Calls go through the hub when it's up and straight to the lights when it isn't.

## Scenes

Scene lists come from Govee's public scene library (cached a week).

- H60B2 takes scenes over the LAN (`ptReal` packets).
- H6008 ignores `ptReal`, so its scenes use Govee's cloud API and need a free key (Govee Home → Profile → Settings
  → Apply for API Key). Paste it on the phone page or set `GOVEE_API_KEY`.
- Other models: add yours to `LAN_SCENE_MODELS` in `govee/govee_scenes.py` if `ptReal` works for it.

## Config

| Variable | Purpose | Default |
| --- | --- | --- |
| `GOVEE_LIGHTS_HOME` | lights list, scene cache, saved key | `~/Library/Application Support/govee-lights`, or `~/.config/govee-lights` on Linux |
| `GOVEE_API_KEY` | cloud key: H6008 scenes, Govee app names, missing-light check | the key saved from the phone page |
| `GOVEE_LIGHT_IPS` | lights to ask directly when multicast is blocked | none |
| `GOVEE_HUB` | hub URL for the MCP server; empty means go direct | `http://127.0.0.1:8737` |
| `GOVEE_BLUETOOTH_APP` | where the Bluetooth app is | `mac/Govee Bluetooth.app` |
| `LIGHTS_PAGE_PORT` | hub port (the menu bar app expects 8737) | `8737` |

`lights.json` is written for you; `lights.example.json` shows the format.

## Protocol notes

- Scan: multicast to 239.255.255.250:4001 on each interface, plus unicast to `GOVEE_LIGHT_IPS`; replies arrive on :4002.
- Commands go to :4003 (`turn`, `brightness`, `colorwc`); `devStatus` reads state back.
- Each change is verified and resent to lights that missed it, up to twice.
- Only one process can listen on :4002 at a time, so it binds briefly per call.

## Tests

```bash
python3 -m unittest discover -s govee -v
```

Fake light on localhost, a fake Govee API and a fake Bluetooth app; never touches real devices.

Not affiliated with Govee. MIT license.
