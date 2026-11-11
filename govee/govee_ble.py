"""Govee lights that only have Bluetooth (like the H617A strip), from a Mac.

Python can't reach Bluetooth with the standard library, so the bytes travel through a small Mac app,
mac/Govee Bluetooth.app (built by mac/build.sh). This module builds Govee's 20-byte packets and reads the replies:

    33 01 <0|1>                            power
    33 04 <0-100>                          brightness
    33 05 15 01 <r g b> 00*5 ff 7f         color, on every segment (RGBIC strips)
    aa 01 / aa 04                          ask for power / brightness; the light answers aa 01 <0|1>, aa 04 <n>

Each packet is padded to 19 bytes and ends with the XOR of those bytes. Scenes use the same a3 / 33 05 04 packets
that Govee's LAN "ptReal" command carries (see govee_scenes.lan_packets). Tested on an H617A, 2026-11-09.

Bluetooth reaches only lights in range of this Mac, and a light takes one connection at a time: while the Govee app
on a phone is connected to it, commands from here can't get in.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.environ.get("GOVEE_BLUETOOTH_APP") or os.path.join(HERE, "..", "mac", "Govee Bluetooth.app")
NAME = re.compile(r"^(?:Govee|ihoment|Minger)_(H[0-9A-Z]{4,6})_([0-9A-F]{4})$|^GV(H[0-9A-Z]{4})([0-9A-F]{4})$",
                  re.IGNORECASE)
STATE_TTL = 120  # seconds a read state stands in for a fresh one, so a refresh doesn't connect every time
_states: dict[str, tuple[float, dict]] = {}


class BluetoothError(Exception):
    """A Bluetooth problem worth telling the person about, in plain words."""


def available() -> bool:
    return sys.platform == "darwin" and os.path.isdir(APP)


def packet(*body: int) -> bytes:
    """Pad to 19 bytes and add the XOR checksum: one 20-byte Govee packet."""
    data = list(body) + [0] * (19 - len(body))
    check = 0
    for b in data:
        check ^= b
    return bytes(data + [check])


def parse_name(name: str):
    """(model, last four hex digits of its Bluetooth address) from an advertised name like Govee_H617A_0A45 or
    GVH6008EA45, or None for anything else."""
    match = NAME.match(str(name or "").strip())
    if not match:
        return None
    sku, suffix = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(4))
    return sku.upper(), suffix.upper()


def kelvin_rgb(kelvin: int) -> tuple:
    """An RGB stand-in for white light at this temperature, for strips without a white channel command."""
    warm, cool = (255, 147, 41), (201, 226, 255)  # about 2000K and about 9000K
    t = max(0.0, min(1.0, (kelvin - 2000) / 7000))
    return tuple(round(a + (b - a) * t) for a, b in zip(warm, cool))


def packets(change: dict) -> list[bytes]:
    """The packets for a change ({"power", "brightness", "rgb", "kelvin"}, as govee_lan.parse_change makes)."""
    if change.get("power") is False:
        return [packet(0x33, 0x01, 0x00)]
    out = [packet(0x33, 0x01, 0x01)]
    rgb = change.get("rgb") or (kelvin_rgb(change["kelvin"]) if change.get("kelvin") else None)
    if rgb:
        r, g, b = rgb
        out.append(packet(0x33, 0x05, 0x15, 0x01, r, g, b, 0, 0, 0, 0, 0, 0xFF, 0x7F))
    if change.get("brightness"):
        out.append(packet(0x33, 0x04, max(1, min(100, int(change["brightness"])))))
    return out


def segment_packets(colors: list) -> list[bytes]:
    """One color per segment (section of a strip, head of a lamp), as few packets as possible: segments that share
    a color go in one packet, picked out by a bit mask (bit 0 = the first segment, 15 bits)."""
    masks: dict[tuple, int] = {}
    for index, color in enumerate(colors[:15]):
        masks[tuple(color)] = masks.get(tuple(color), 0) | (1 << index)
    return [packet(0x33, 0x05, 0x15, 0x01, r, g, b, 0, 0, 0, 0, 0, mask & 0xFF, mask >> 8)
            for (r, g, b), mask in masks.items()]


ASK = [packet(0xAA, 0x01), packet(0xAA, 0x04)]


def parse_state(lines: list[str]) -> dict:
    """{"onOff", "brightness"} from the light's replies, as far as they go. Color can't be read back."""
    state: dict = {}
    for line in lines:
        try:
            data = bytes.fromhex(line)
        except ValueError:
            continue
        if len(data) >= 3 and data[0] == 0xAA and data[1] == 0x01:
            state["onOff"] = 1 if data[2] else 0
        elif len(data) >= 3 and data[0] == 0xAA and data[1] == 0x04:
            state["brightness"] = data[2]
    return state


def _socket_path() -> str:
    """Where the Bluetooth app listens while it stays running (Unix socket paths must be short)."""
    import govee_lan  # here, not at the top: govee_lan imports this module
    path = os.path.join(govee_lan.DATA_DIR, "bluetooth.sock")
    return path if len(path) < 100 else os.path.join(tempfile.gettempdir(), f"govee-lights-{os.getuid()}.sock")


def _ask_server(args: list[str], timeout: float) -> list[str] | None:
    """Send a command to the running Bluetooth app; None if it isn't running."""
    import json
    import socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(_socket_path())
            s.sendall(json.dumps({"args": args}).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        return [str(line) for line in json.loads(data).get("lines", [])]
    except (OSError, ValueError):
        return None


def _server_lines(args: list[str], timeout: float) -> list[str] | None:
    """The command's output from the staying-connected Bluetooth app, starting it if it isn't running."""
    lines = _ask_server(args, timeout)
    if lines is not None:
        return lines
    subprocess.run(["open", "-g", "-n", APP, "--args", "serve", _socket_path()], capture_output=True, check=False,
                   timeout=10)
    for _ in range(30):  # it takes a moment to start listening
        time.sleep(0.1)
        if os.path.exists(_socket_path()):
            lines = _ask_server(args, timeout)
            if lines is not None:
                return lines
    return None


def _run(args: list[str], timeout: float = 25) -> list[str]:
    """Run a Bluetooth app command and return its output lines. Through the app staying connected when possible
    (a fraction of a second); otherwise as a one-shot run, whose output comes back in a file because `open` can't
    pass output or an exit code back."""
    if not available():
        raise BluetoothError("Bluetooth lights need the Govee Bluetooth app: run mac/build.sh")
    lines = _server_lines(args, timeout)
    if lines is None:
        lines = _one_shot(args, timeout)
    code = next((line.split(" ", 1)[1] for line in reversed(lines) if line.startswith("exit ")), "?")
    if code != "0":
        problem = next((line[6:] for line in lines if line.startswith("error ")), f"it stopped with code {code}")
        raise BluetoothError(problem)
    return [line for line in lines if not line.startswith(("exit ", "error "))]


def _one_shot(args: list[str], timeout: float) -> list[str]:
    with tempfile.TemporaryDirectory() as folder:
        out = os.path.join(folder, "out.txt")
        try:
            subprocess.run(["open", "-W", "-n", APP, "--args", "--out", out, *args], timeout=timeout,
                           capture_output=True, check=False)
        except subprocess.TimeoutExpired:
            raise BluetoothError("the Bluetooth light took too long to answer") from None
        try:
            with open(out) as f:
                lines = [line.strip() for line in f if line.strip()]
        except OSError:
            raise BluetoothError("the Govee Bluetooth app didn't start") from None
    return lines


def scan(seconds: float = 3) -> list[dict]:
    """The Govee lights heard over Bluetooth: [{"id", "name", "sku", "suffix", "rssi"}]. Every Govee light
    advertises, Wi-Fi ones too, so the caller drops the ones it already reaches over the network."""
    import json
    found = []
    for line in _run(["scan", str(seconds)], timeout=seconds + 20):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        parsed = parse_name(item.get("name", ""))
        if parsed and item.get("id"):
            found.append({**item, "sku": parsed[0], "suffix": parsed[1]})
    return found


def send(device: str, data: list[bytes], read: bool = False) -> dict:
    """Write packets to one light. With read, also ask for its power and brightness and return them."""
    lines = _run(["send", device] + [p.hex() for p in data + (ASK if read else [])] + (["--read"] if read else []))
    state = parse_state(lines) if read else {}
    if state:
        _states[device] = (time.monotonic(), state)
    return state


def apply(device: str, change: dict) -> dict:
    """Send a change and read back what the light reports (power and brightness)."""
    return send(device, packets(change), read=True)


def last_state(device: str) -> dict:
    """The last power and brightness read from the light, however old, without connecting; {} if none."""
    seen = _states.get(device)
    return dict(seen[1]) if seen else {}


def state(device: str) -> dict:
    """The light's power and brightness: a recent reading if there is one, else a fresh one; {} if it can't be
    reached."""
    seen = _states.get(device)
    if seen and time.monotonic() - seen[0] < STATE_TTL:
        return dict(seen[1])
    try:
        return send(device, [], read=True)
    except BluetoothError:
        return {}

