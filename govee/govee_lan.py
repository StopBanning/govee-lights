#!/usr/bin/env python3
"""Control Govee lights over the home network: no cloud, no key.

Uses Govee's LAN API. Each light needs LAN Control switched on in the Govee Home app (tap the light, then the
gear). Lights answer on UDP port 4002, so this module only listens there for a moment per call.

Every light seen so far is kept in lights.json in the data folder (see DATA_DIR), keyed by Govee device ID, along
with the name and room it has been given. Addresses come from DHCP and can change; a fresh scan updates them.

Run `python3 govee_lan.py --help` for the command line, or use govee_mcp.py as an MCP server.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import govee_ble

SCAN_ADDR = ("239.255.255.250", 4001)
LISTEN_PORT = 4002
CONTROL_PORT = 4003


def _data_dir() -> str:
    """Where this keeps its own files: $GOVEE_LIGHTS_HOME, else ~/Library/Application Support/govee-lights on a
    Mac and ~/.config/govee-lights elsewhere. Kept out of the code folder, so the code can be shared as it is."""
    if os.environ.get("GOVEE_LIGHTS_HOME"):
        return os.path.expanduser(os.environ["GOVEE_LIGHTS_HOME"])
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/govee-lights")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "govee-lights")


DATA_DIR = _data_dir()
LIGHTS_FILE = os.path.join(DATA_DIR, "lights.json")

# What to call each model, so lights get labels like "strip 2" and can be picked out by kind ("the strips").
# A model missing here falls back to its family (MODEL_FAMILIES), then to "light". Labels only: every model with
# LAN Control is found and controlled the same way.
KINDS = {
    **dict.fromkeys(["H6008", "H6009"], "bulb"),
    **dict.fromkeys(["H6072", "H6076", "H6078", "H607C", "H60B2"], "floor lamp"),
    **dict.fromkeys(["H6022", "H6051"], "table lamp"),
    **dict.fromkeys(["H6046", "H6047", "H6056"], "light bar"),
    **dict.fromkeys(["H6061", "H6062", "H6065", "H6066", "H606A"], "panel"),
    **dict.fromkeys(["H60A0", "H60A1"], "ceiling light"),
    **dict.fromkeys(["H6168", "H6199"], "tv backlight"),
    **dict.fromkeys(["H7020", "H7021", "H7028", "H7041", "H7042", "H7050", "H7055", "H7060", "H705A", "H705B",
                     "H705C"], "outdoor light"),
    "H70B1": "curtain light",
}
MODEL_FAMILIES = {"H61": "strip", "H70": "outdoor light"}  # by the start of the model number; add yours
TEMPERATURES = {"warm": 2700, "soft": 3000, "neutral": 4000, "cool": 5500, "daylight": 6500}
WHITES = {"white": 4000, "warm white": 2700, "soft white": 3000, "cool white": 5500}
COLORS = {
    "red": (255, 0, 0), "orange": (255, 100, 0), "yellow": (255, 190, 0), "green": (0, 255, 0),
    "teal": (0, 200, 140), "cyan": (0, 255, 255), "blue": (0, 0, 255), "purple": (130, 0, 255),
    "magenta": (255, 0, 255), "pink": (255, 50, 140),
}
KELVIN_RANGE = (2000, 9000)
ALL_WORDS = {"", "all", "everything", "lights", "all lights", "every light"}


class GoveeError(Exception):
    """A problem worth telling the person about, in plain words."""


@dataclass
class Light:
    device: str  # Govee device ID; never changes
    sku: str  # model, like H6008
    ip: str
    name: str = ""
    room: str = ""
    label: str = ""  # the name if there is one, else "bulb 3" and so on
    bluetooth: bool = False  # reached over Bluetooth (govee_ble), not the network; then device is a Mac-side id

    @property
    def kind(self) -> str:
        return kind_of(self.sku)


def kind_of(sku: str) -> str:
    """"bulb", "strip" and so on for a model number; "light" when it's unknown."""
    sku = str(sku or "").upper()
    return KINDS.get(sku) or next((kind for start, kind in MODEL_FAMILIES.items() if sku.startswith(start)), "light")


# --- the wire protocol ---

def _packet(cmd: str, data: dict) -> bytes:
    return json.dumps({"msg": {"cmd": cmd, "data": data}}).encode()


def _parse(data: bytes) -> dict:
    try:
        msg = json.loads(data)["msg"]
    except (ValueError, KeyError, TypeError):
        return {}
    return msg if isinstance(msg, dict) else {}


def _sendto(sock: socket.socket, packet: bytes, addr: tuple) -> None:
    try:
        sock.sendto(packet, addr)
    except OSError as e:
        raise GoveeError(f"can't reach the home network ({e.strerror or e}). On a Mac, the app running this may "
                         "need Local Network access in System Settings > Privacy & Security.") from None


def _listener(timeout: float) -> socket.socket:
    """A socket on the port the lights answer to. Retries briefly if another program has it."""
    for _attempt in range(5):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            s.bind(("", LISTEN_PORT))
        except OSError:
            s.close()
            time.sleep(0.2)
            continue
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(timeout)
        return s
    raise GoveeError(f"UDP port {LISTEN_PORT} is busy, so the lights can't answer. Is another Govee program running?")


def local_addresses() -> list[str]:
    """This computer's private IPv4 addresses, one per network it's on (Wi-Fi, Ethernet, a VPN...).

    The scan goes out on each, because the system's default network isn't always the one the lights are on: with
    a VPN up, a plain multicast leaves through the VPN and no light hears it. Empty if they can't be listed."""
    commands = [["/sbin/ifconfig"], ["ifconfig"], ["ip", "-o", "-4", "addr", "show"]]
    for command in commands:
        try:
            out = subprocess.run(command, capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        found = []
        for word in re.findall(r"\binet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", out):
            try:
                ip = ipaddress.ip_address(word)
            except ValueError:
                continue
            if ip.is_private and not ip.is_loopback and not ip.is_link_local and word not in found:
                found.append(word)
        if out:
            return found
    return []


def manual_addresses() -> list[str]:
    """Lights to ask directly, from $GOVEE_LIGHT_IPS (commas or spaces), for networks that block multicast."""
    return [ip for ip in re.split(r"[,\s]+", os.environ.get("GOVEE_LIGHT_IPS", "")) if ip]


def _probe(s: socket.socket, probe: bytes, interfaces: list[str], direct: list[str]) -> None:
    """One round of the scan: the multicast on every network, then a direct ask to each listed light."""
    sent = False
    for address in interfaces:
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
            s.sendto(probe, SCAN_ADDR)
            sent = True
        except OSError:
            continue  # a network that went away, or one that can't multicast
    if not sent:
        _sendto(s, probe, SCAN_ADDR)  # the system's default network; raises a plain-words error if it can't
    for ip in direct:
        try:
            s.sendto(probe, (ip, SCAN_ADDR[1]))
        except OSError:
            pass


def scan(timeout: float = 1.5, expect: int = 0) -> list[Light]:
    """Find the lights that have LAN Control on. Stops early once `expect` lights have answered."""
    found: dict[str, Light] = {}
    probe = _packet("scan", {"account_topic": "reserve"})
    interfaces, direct = local_addresses(), manual_addresses()
    with _listener(0.2) as s:
        _probe(s, probe, interfaces, direct)
        start, resent = time.monotonic(), False
        while time.monotonic() - start < timeout and not (expect and len(found) >= expect):
            if not resent and time.monotonic() - start > timeout / 2:
                _probe(s, probe, interfaces, direct)
                resent = True
            try:
                data, (ip, _port) = s.recvfrom(4096)
            except socket.timeout:
                continue
            msg = _parse(data)
            info = msg.get("data") or {}
            if msg.get("cmd") == "scan" and info.get("device"):
                # The H60B2 leaves "ip" out of its reply, so trust the packet's source address.
                found[info["device"]] = Light(info["device"], info.get("sku", "?"), ip)
    return list(found.values())


def status(lights: list[Light], timeout: float = 1.0) -> dict[str, dict]:
    """Ask lights for their state. Returns {device ID: state} for the ones that answered. Bluetooth lights report
    power and brightness only."""
    states: dict[str, dict] = {}
    for light in lights:
        if light.bluetooth:
            state = govee_ble.state(light.device)
            if state:
                states[light.device] = state
    by_ip = {light.ip: light for light in lights if not light.bluetooth}
    if not by_ip:
        return states
    ask = _packet("devStatus", {})
    with _listener(0.1) as s:
        for _round in range(2):
            for ip, light in by_ip.items():
                if light.device not in states:
                    _sendto(s, ask, (ip, CONTROL_PORT))
            end = time.monotonic() + timeout / 2
            while time.monotonic() < end and not all(light.device in states for light in by_ip.values()):
                try:
                    data, (ip, _port) = s.recvfrom(4096)
                except socket.timeout:
                    continue
                msg = _parse(data)
                if msg.get("cmd") == "devStatus" and ip in by_ip:
                    states[by_ip[ip].device] = msg.get("data") or {}
            if all(light.device in states for light in by_ip.values()):
                break
    return states


def _messages(change: dict) -> list[bytes]:
    """The packets for a change, in the order to send them."""
    if change.get("power") is False:
        return [_packet("turn", {"value": 0})]
    packets = [_packet("turn", {"value": 1})]
    if change.get("kelvin"):
        packets.append(_packet("colorwc", {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": change["kelvin"]}))
    elif change.get("rgb"):
        r, g, b = change["rgb"]
        packets.append(_packet("colorwc", {"color": {"r": r, "g": g, "b": b}, "colorTemInKelvin": 0}))
    if change.get("brightness"):
        packets.append(_packet("brightness", {"value": change["brightness"]}))
    return packets


def send(lights: list[Light], change: dict) -> None:
    """Send a change without checking it. A Bluetooth light that can't be reached is skipped here; apply() and
    the read-backs are where it shows."""
    for light in lights:
        if light.bluetooth:
            try:
                govee_ble.send(light.device, govee_ble.packets(change))
            except govee_ble.BluetoothError:
                pass
    network = [light for light in lights if not light.bluetooth]
    if not network:
        return
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for packet in _messages(change):
            for light in network:
                _sendto(s, packet, (light.ip, CONTROL_PORT))
            time.sleep(0.05)


def matches(state: dict, change: dict) -> bool:
    """Whether a light's reported state shows that the change took."""
    if not state:
        return False
    if change.get("power") is False:
        return state.get("onOff") == 0
    if state.get("onOff") != 1:
        return False
    if change.get("brightness") and state.get("brightness") != change["brightness"]:
        return False
    if change.get("kelvin") and abs((state.get("colorTemInKelvin") or 0) - change["kelvin"]) > 200:
        return False
    if change.get("rgb"):
        color = state.get("color") or {}
        if state.get("colorTemInKelvin") or any(abs(color.get(k, -999) - v) > 10 for k, v in zip("rgb", change["rgb"])):
            return False
    return True


def matches_bluetooth(state: dict, change: dict) -> bool:
    """matches() for a Bluetooth light, which reports power and brightness but not color."""
    if not state:
        return False
    if change.get("power") is False:
        return state.get("onOff") == 0
    return state.get("onOff") == 1 and (not change.get("brightness") or state.get("brightness") == change["brightness"])


def _apply_bluetooth(lights: list[Light], change: dict) -> list[tuple[Light, dict]]:
    """Each Bluetooth light: send, read back, and try once more if it didn't take. Returns the ones that didn't."""
    failed = []
    for light in lights:
        state: dict = {}
        for _attempt in range(2):
            try:
                state = govee_ble.apply(light.device, change)
            except govee_ble.BluetoothError:
                state = {}
            if matches_bluetooth(state, change):
                break
        else:
            failed.append((light, state))
    return failed


def apply(lights: list[Light], change: dict) -> list[tuple[Light, dict]]:
    """Send a change and check that it took, resending to stragglers up to twice (Wi-Fi bulbs drop packets).

    Returns (light, the state it last reported, or {} if it never answered) for each light that didn't confirm."""
    failed = _apply_bluetooth([light for light in lights if light.bluetooth], change)
    lights = [light for light in lights if not light.bluetooth]
    if not lights:
        return failed
    send(lights, change)
    pending, refreshed = list(lights), False
    for attempt, wait in enumerate((0.3, 0.6, 1.0)):
        time.sleep(wait)
        heard = status(pending)
        pending = [light for light in pending if not matches(heard.get(light.device, {}), change)]
        if not pending or attempt == 2:
            break
        if not refreshed and any(light.device not in heard for light in pending):
            pending, refreshed = _refresh_addresses(pending), True  # a silent light may have a new address
        send(pending, change)
    return failed + [(light, heard.get(light.device, {})) for light in pending]


# --- the list of known lights ---

def _read_registry() -> dict:
    try:
        with open(LIGHTS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _write_registry(registry: dict) -> None:
    os.makedirs(os.path.dirname(LIGHTS_FILE), mode=0o700, exist_ok=True)
    tmp = f"{LIGHTS_FILE}.{os.getpid()}.tmp"  # then a rename, so a reader never sees half a file
    with open(tmp, "w") as f:
        json.dump(registry, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, LIGHTS_FILE)


def _labelled(registry: dict) -> list[Light]:
    lights = [Light(device, entry.get("sku", "?"), entry.get("ip", ""), entry.get("name", ""), entry.get("room", ""),
                    bluetooth=bool(entry.get("bluetooth")))
              for device, entry in registry.items()]
    lights.sort(key=lambda light: (light.kind, light.device))
    counts: dict[str, int] = {}
    for light in lights:
        counts[light.kind] = counts.get(light.kind, 0) + 1
        light.label = light.name or f"{light.kind} {counts[light.kind]}"
    return lights


def known_lights(rescan: bool = False) -> list[Light]:
    """Every light seen so far, with names. Scans when asked to, or when none are known yet."""
    registry = _read_registry()
    if rescan or not registry:
        changed = False
        heard_over_bluetooth: list = []
        listener = None
        if govee_ble.available():  # alongside the network scan, which is shorter
            def listen():
                try:
                    heard_over_bluetooth.extend(govee_ble.scan())
                except govee_ble.BluetoothError:
                    pass
            listener = threading.Thread(target=listen, daemon=True)
            listener.start()
        for light in scan():
            entry = registry.setdefault(light.device, {})
            if entry.get("ip") != light.ip or entry.get("sku") != light.sku:
                entry.update(ip=light.ip, sku=light.sku)
                changed = True
        if listener:
            listener.join(timeout=30)
        changed = _add_bluetooth_lights(registry, heard_over_bluetooth) or changed
        changed = _name_from_govee_app(registry) or changed
        if changed:
            _write_registry(registry)
    return _labelled(registry)


def _same_light(sku: str, suffix: str, device: str) -> bool:
    """Whether a light heard over Bluetooth is one already reached over the network. Its advertised name ends in the
    last four hex digits of its Bluetooth address, which is the Wi-Fi address (the end of the device ID) or one
    more."""
    try:
        wifi = int(device.replace(":", "")[-4:], 16)
        return abs(int(suffix, 16) - wifi) <= 2
    except ValueError:
        return False


def _add_bluetooth_lights(registry: dict, heard: list[dict]) -> bool:
    """Keep the Bluetooth-only lights among those heard. True if anything changed."""
    network = [(entry.get("sku"), device) for device, entry in registry.items() if not entry.get("bluetooth")]
    changed = False
    for item in heard:
        if any(sku == item["sku"] and _same_light(item["sku"], item["suffix"], device) for sku, device in network):
            continue
        entry = registry.setdefault(item["id"], {})
        if entry.get("sku") != item["sku"] or not entry.get("bluetooth"):
            entry.update(sku=item["sku"], bluetooth=True, ble_name=item.get("name", ""))
            changed = True
    return changed


def account_devices() -> list[dict]:
    """The lights in the person's Govee account ({"device", "sku", "deviceName"}), or [] without an API key."""
    import govee_scenes  # here, not at the top: govee_scenes imports this module
    return govee_scenes.account_devices()


def _name_from_govee_app(registry: dict) -> bool:
    """Give each unnamed light the name it has in the Govee Home app, when a key lets us ask. Names given here
    are never replaced, and a name two lights share in the app is skipped. True if anything changed."""
    taken = {entry.get("name", "").lower() for entry in registry.values() if entry.get("name")}
    names: dict[str, str] = {}
    counts: dict[str, int] = {}
    for item in account_devices():
        name = " ".join(str(item.get("deviceName") or "").split())
        if item.get("device") and name:
            names[item["device"]] = name
            counts[name.lower()] = counts.get(name.lower(), 0) + 1
    changed = False
    for device, entry in registry.items():
        name = names.get(device, "")
        if name and not entry.get("name") and counts[name.lower()] == 1 and name.lower() not in taken:
            entry["name"] = name
            taken.add(name.lower())
            changed = True
    return changed


def _refresh_addresses(lights: list[Light]) -> list[Light]:  # network lights only
    current = {light.device: light for light in known_lights(rescan=True)}
    return [current.get(light.device, light) for light in lights]


def _keys(light: Light) -> set:
    kind = light.kind.lower()
    keys = {light.label.lower(), light.ip, light.device.lower(), light.sku.lower(), kind, kind + "s"}
    if light.name:
        keys.add(light.name.lower())
    if light.room:
        room = light.room.lower()
        keys |= {room, room + " light", room + " lights"}
    return keys


def resolve(target, lights: list[Light]) -> list[Light]:
    """Pick lights by name, room, label ("bulb 3"), kind ("bulbs", "floor lamps"), address or device ID.

    Several can be given, separated by commas."""
    wanted = " ".join(str(target or "").lower().split())
    if wanted.startswith("the "):
        wanted = wanted[4:]
    if wanted in ALL_WORDS:
        return list(lights)
    chosen: list[Light] = []
    for part in (p.strip() for p in wanted.split(",")):
        if part.startswith("the "):
            part = part[4:]
        if not part:
            continue
        hits = [light for light in lights if part in _keys(light)]
        if not hits:
            choices = []
            for light in lights:
                for word in (light.label, light.room, light.kind + "s"):
                    if word and word not in choices:
                        choices.append(word)
            raise GoveeError(f'no light called "{part}". Try: all, ' + ", ".join(choices))
        chosen += [light for light in hits if light not in chosen]
    return chosen


def _one(target: str) -> Light:
    lights = resolve(target, known_lights())
    if not lights:
        raise GoveeError("no Govee lights are known yet; check that LAN Control is on in the Govee Home app")
    if len(lights) > 1:
        raise GoveeError(f'"{target}" matches {len(lights)} lights; pick one, like "{lights[0].label}"')
    return lights[0]


# --- what a person asks for ---

def parse_temperature(value) -> int:
    word = str(value).strip().lower()
    if word in TEMPERATURES:
        return TEMPERATURES[word]
    try:
        kelvin = int(float(word.rstrip("k").strip()))
    except ValueError:
        raise GoveeError("temperature must be kelvin (2000 to 9000) or one of: " + ", ".join(TEMPERATURES)) from None
    return max(KELVIN_RANGE[0], min(KELVIN_RANGE[1], kelvin))


def parse_color(value) -> dict:
    """{"kelvin": n} for the whites, {"rgb": (r, g, b)} for colors."""
    word = " ".join(str(value).lower().split())
    if word in WHITES:
        return {"kelvin": WHITES[word]}
    if word in TEMPERATURES:
        return {"kelvin": TEMPERATURES[word]}
    if word in COLORS:
        return {"rgb": COLORS[word]}
    digits = word.lstrip("#")
    if len(digits) == 6 and all(c in "0123456789abcdef" for c in digits):
        return {"rgb": tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))}
    parts = [p.strip() for p in word.split(",")]
    if len(parts) == 3 and all(p.isdigit() and int(p) <= 255 for p in parts):
        return {"rgb": tuple(int(p) for p in parts)}
    raise GoveeError(f'unknown color "{value}". Use a hex code like #ff8800 or one of: '
                     + ", ".join(list(COLORS) + list(WHITES)))


def parse_change(power=None, brightness=None, color=None, temperature=None) -> dict:
    change: dict = {}
    if power not in (None, ""):
        word = str(power).strip().lower()
        if word not in ("on", "off"):
            raise GoveeError('power must be "on" or "off"')
        change["power"] = word == "on"
        if word == "off":
            return change
    if brightness not in (None, ""):
        try:
            level = int(str(brightness).strip().rstrip("%"))
        except ValueError:
            raise GoveeError("brightness must be a number from 1 to 100") from None
        if not 1 <= level <= 100:
            raise GoveeError("brightness must be from 1 to 100; to turn lights off, use power off")
        change["brightness"] = level
    if color and temperature:
        raise GoveeError("give a color or a temperature, not both")
    if temperature not in (None, ""):
        change["kelvin"] = parse_temperature(temperature)
    elif color:
        change.update(parse_color(color))
    if not change:
        raise GoveeError("nothing to change: give power, brightness, color or temperature")
    return change


def where(light: Light) -> str:
    return "over Bluetooth" if light.bluetooth else f"at {light.ip}"


def describe(state: dict) -> str:
    if not state:
        return "didn't answer"
    if state.get("onOff") != 1:
        return "off"
    if "color" not in state and "colorTemInKelvin" not in state:  # a Bluetooth light: no color to report
        return f"on, {state.get('brightness', '?')}%"
    kelvin = state.get("colorTemInKelvin") or 0
    color = state.get("color") or {}
    shade = f"white {kelvin}K" if kelvin else "color #{:02x}{:02x}{:02x}".format(
        color.get("r", 0), color.get("g", 0), color.get("b", 0))
    return f"on, {state.get('brightness', '?')}%, {shade}"


def _summary(change: dict) -> str:
    if change.get("power") is False:
        return "Off"
    parts = []
    if change.get("kelvin"):
        parts.append(f"white {change['kelvin']}K")
    if change.get("rgb"):
        parts.append("color #{:02x}{:02x}{:02x}".format(*change["rgb"]))
    if change.get("brightness"):
        parts.append(f"{change['brightness']}%")
    return "On, " + ", ".join(parts) if parts else "On"


NO_LIGHTS = "No Govee lights answered. Check that LAN Control is on for each light in the Govee Home app."


def list_lights() -> str:
    lights = known_lights(rescan=True)
    if not lights:
        return NO_LIGHTS + "".join("\n" + line for line in _not_on_the_network([]))
    states = status(lights)
    lines = [f"{len(lights)} lights:"]
    for light in lights:
        room = f" ({light.room})" if light.room else ""
        lines.append(f"- {light.label}{room}: {describe(states.get(light.device, {}))}"
                     f"  [{light.sku} {where(light)}, id {light.device}]")
    lines += _not_on_the_network(lights)
    return "\n".join(lines)


def _not_on_the_network(lights: list[Light]) -> list[str]:
    """Lights the Govee account has that didn't answer here: LAN Control is off, or the model doesn't have it."""
    here = {light.device for light in lights}
    missing = [item for item in account_devices() if item.get("device") and item["device"] not in here]
    if not missing:
        return []
    return [f"Also in your Govee account, but not answering on this network ({len(missing)}):"] + [
        f"- {item.get('deviceName') or item['device']} ({item.get('sku', '?')})" for item in missing] + [
        "Turn on LAN Control for them in the Govee Home app (the light, then the gear). Bluetooth-only models "
        "don't have it."]


def set_lights(target="all", power=None, brightness=None, color=None, temperature=None) -> str:
    return set_lights_result(target, power, brightness, color, temperature)["text"]


def set_lights_result(target="all", power=None, brightness=None, color=None, temperature=None) -> dict:
    """set_lights, plus how many lights were asked and which ones didn't confirm."""
    change = parse_change(power, brightness, color, temperature)
    lights = resolve(target, known_lights())
    if not lights:
        return {"text": NO_LIGHTS, "total": 0, "failed": []}
    failed = apply(lights, change)
    count = f"{len(lights)} light" + ("" if len(lights) == 1 else "s")
    text = f"{_summary(change)} for {count}: {len(lights) - len(failed)} confirmed."
    if failed:
        text += " Not confirmed: " + "; ".join(
            f"{light.label} reports {describe(state)}" if state
            else f"{light.label} didn't answer (switched off at the wall?)" for light, state in failed) + "."
    if change.get("power") is False and any(v not in (None, "") for v in (brightness, color, temperature)):
        text += " The other settings were ignored because the lights are now off."
    return {"text": text, "total": len(lights), "failed": [light.label for light, _state in failed]}


def identify_light(target: str) -> str:
    """Blink one light twice, then leave it on or off as it was."""
    light = _one(target)
    state = status([light]).get(light.device)
    if not state:
        raise GoveeError(f"{light.label} didn't answer, so it can't be blinked")
    was_on = 1 if state.get("onOff") == 1 else 0
    if light.bluetooth:
        try:  # one connection for all four steps would be quicker, but each step is its own short write
            for value in (1 - was_on, was_on, 1 - was_on, was_on):
                govee_ble.send(light.device, [govee_ble.packet(0x33, 0x01, value)])
                time.sleep(0.3)
        except govee_ble.BluetoothError as e:
            raise GoveeError(f"{light.label}: {e}") from None
        return f"Blinked {light.label} ({light.sku} over Bluetooth) twice and left it {'on' if was_on else 'off'}."
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for value in (1 - was_on, was_on, 1 - was_on, was_on):
            _sendto(s, _packet("turn", {"value": value}), (light.ip, CONTROL_PORT))
            time.sleep(0.8)
        if status([light]).get(light.device, {}).get("onOff") != was_on:
            _sendto(s, _packet("turn", {"value": was_on}), (light.ip, CONTROL_PORT))
    return f"Blinked {light.label} ({light.sku} {where(light)}) twice and left it {'on' if was_on else 'off'}."


def name_light(target: str, name: str, room: str = "") -> str:
    light = _one(target)
    name, room = " ".join(str(name).split()), " ".join(str(room or "").split())
    if not name:
        raise GoveeError("give the light a name")
    registry = _read_registry()
    for device, entry in registry.items():
        if device != light.device and entry.get("name", "").lower() == name.lower():
            raise GoveeError(f'"{name}" is already another light\'s name. Use a room to group lights.')
    entry = registry.setdefault(light.device, {"sku": light.sku, "ip": light.ip})
    entry["name"] = name
    if room:
        entry["room"] = room
    _write_registry(registry)
    return f'{light.label} is now called "{name}"' + (f" (room: {room})" if room else "") + "."


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Control Govee lights on the home network.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="scan for lights and show their state")
    p = sub.add_parser("set", help="change lights")
    p.add_argument("target", nargs="?", default="all",
                   help='"all" (the default), a name, a room, "bulbs" or "floor lamps"; commas for several')
    p.add_argument("--power", choices=["on", "off"])
    p.add_argument("--brightness", metavar="1-100")
    p.add_argument("--color", help='a name like "blue" or "warm white", or a hex code like "#ff8800"')
    p.add_argument("--temp", dest="temperature", help="warm, soft, neutral, cool, daylight, or kelvin (2000-9000)")
    p = sub.add_parser("blink", help="blink one light twice to see which one it is")
    p.add_argument("target")
    p = sub.add_parser("name", help="give one light a name, and optionally a room")
    p.add_argument("target")
    p.add_argument("name")
    p.add_argument("--room", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            print(list_lights())
        elif args.command == "set":
            print(set_lights(args.target, args.power, args.brightness, args.color, args.temperature))
        elif args.command == "blink":
            print(identify_light(args.target))
        else:
            print(name_light(args.target, args.name, args.room))
    except GoveeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
